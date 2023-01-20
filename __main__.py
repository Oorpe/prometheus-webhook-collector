from flask import Flask, Response, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from prometheus_client import (
    make_wsgi_app,
    write_to_textfile,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
import re
from jsonpath_ng import jsonpath, parse
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

# cache in-memory
metrics_cache = {}


class CustomCollector(object):
    def collect(self):

        for metric_name, cached_metric in metrics_cache.items():

            help = cached_metric.get("help", "")
            labels = cached_metric.get("labels", {})
            value = cached_metric.get("value", None)
            c = GaugeMetricFamily(metric_name, help, labels=labels.keys())

            c.add_metric(labels.values(), float(value))
            yield c


REGISTRY.register(CustomCollector())

# Create my app
app = Flask(__name__)

debug = True

# metric field is a fnmatch matcher (simple globbing)
# extractors are jsonpath expressions (multiple allowed, only labels field uses more than one though)
# labels extractor
config = {
    "metric_extractors": [
        {
            "metric": "/odalogs_.*/",
            "help": ["$.event.fields.help"],
            "type": ["$.event.fields.type"],
            "labels": [
                ["$.event.fields[?(`this` =~ /label_.*/)]"],
                ["$.backlog[?(@.field_of_note)]"]
            ],
            "value": ["$.event.message.`sub(/\((\d+)\)/,\\1)`"],
        }
    ]
}

# @app.route("/test", methods=["GET"])
# def test():
#   return "yay"


def run_extractor(extractor, data):
    def execute(line="", data=None):
        if line.startswith("/") and line.endswith("/"):
            return re.search(line.strip("/"), data).groups()
        return parse(line).find(data)

    return [match.value for line in extractor for match in execute(line, data)]


@app.route("/webhook/<metric_name>", methods=["POST"])
def receive_webhook_request(metric_name):
    """receive a webhook request with data"""

    metric_extractor = next(
        (c for c in config["metric_extractors"] if re.match(c["metric"].strip("/"), metric_name)),
        None,
    )

    data = request.json
    help = run_extractor(metric_extractor["help"], data)
    labels = run_extractor(metric_extractor["labels"], data)
    value = run_extractor(metric_extractor["value"], data)
    # cache value for scraper
    metrics_cache[metric_name] = {"help": help, "value": value, "labels": labels}

    registry = CollectorRegistry()
    g = Gauge(metric_name, help, registry=registry, labelnames=labels.keys())
    g.labels(**labels).set(value)

    # parameterize which output - textfile/pushgateway/scrapeable metrics path
    write_to_textfile(f"/opt/textfile/webhook_stuff/{metric_name}.prom", registry)

    res = (
        ""
        if not debug
        else generate_latest(registry.restricted_registry([metric_name]))
    )

    return Response(res, status=200, mimetype="application/json")


# Add prometheus wsgi middleware to route /metrics requests
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/metrics": make_wsgi_app()})
