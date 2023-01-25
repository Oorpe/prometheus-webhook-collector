from flask import Flask, Response, request, render_template
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from prometheus_client import (
    make_wsgi_app,
    write_to_textfile,
    CollectorRegistry,
    metrics,
    Counter,
    Gauge,
    Info,
    REGISTRY,
)
from prometheus_client.metrics import _get_use_created
from functools import reduce
import yaml, json, os, re, cachetools, jmespath, collections

# disable _created metrics
os.environ["PROMETHEUS_DISABLE_CREATED_SERIES"] = "True"
metrics._use_created = _get_use_created()

# monkey-patch a few sane basics to jmespath
class CustomFunctions(jmespath.functions.Functions):
    @jmespath.functions.signature({"types": ["object"]})
    def _func_items(self, arg={}):
        return [[key, value] for key, value in arg.items()]

    @jmespath.functions.signature({"types": ["array"]})
    def _func_to_object(self, pairs):
        return dict(pairs)


# attach custom functions
options = jmespath.Options(custom_functions=CustomFunctions(), dict_cls=collections.OrderedDict)


# Create flask app
app = Flask(__name__)

# print some extra debug information
debug = True

# load config from file
config = {}
with open("config.yaml", "r") as f:
    config = yaml.load(f, Loader=yaml.Loader)

if debug:
    print(yaml.dump(config, Dumper=yaml.Dumper))

# basic defaults for some settings
defaults = {
    "textfile_dir": "/var/lib/node_exporter/textfile_collector",
    "webhook_basepath": "/webhook",
    "output": {"scrapeable": True, "textfile": False},
    "exporter_metrics": False,
    "cache": {"max_size": 128, "ttl": 600},
}


class Cache(cachetools.TTLCache):
    """Simple TTLCache wrapper class that disposes of popped metric objects properly (unregister & clear)"""

    def pop(self, key):
        value = super().pop(key)
        if "instance" in value:
            value["instance"].clear()
            registry.unregister(value["instance"])
        return value


# cache in-memory, simple ttlcache setup to avoid memory leak on faults
cs = config.get("cache", defaults["cache"])
# configurable size (in dict keys) and ttl for individual values, oldest get purged when max_size reached
metrics_cache = Cache(
    maxsize=cs.get("max_size", defaults["cache"]["max_size"]),
    ttl=cs.get("ttl", defaults["cache"]["ttl"]),
)

textfile_dir = config.get("textfile_dir", defaults["textfile_dir"])
webhook_basepath = config.get("webhook_basepath", defaults["webhook_basepath"])
output = config.get("output", defaults["output"])
exporter_metrics = config.get("exporter_metrics", defaults["exporter_metrics"])

registry = CollectorRegistry(auto_describe=True)

# include default exporter metrics
if exporter_metrics:
    registry = REGISTRY


def run_extractor(extractors, data, default=None):

    if not isinstance(extractors, list):
        extractors = [extractors]

    extracted = []
    for extractor in extractors:
        prev = data
        if not isinstance(extractor, list):
            extractor = [extractor]
        # if nested list, inner list want prev result as input
        for partial in extractor:
            if not partial:
                prev = default
            elif isinstance(partial, str) and partial.startswith("/"):
                prev = re.search(str(partial).strip("/"), json.dumps(prev))
                if isinstance(prev, re.Match):
                    prev = next(iter(prev.groups(default)))
            else:
                prev = jmespath.search(str(partial), prev, options)
                if not prev:
                    prev = default
        extracted.append(prev)

    return extracted[0] if len(extracted) == 1 else extracted


def setup_metric(m_type, metric_name, help, labels, value, old_instance=None):

    assert isinstance(m_type, str)
    label_keys, label_values = list(zip(*labels.items()))

    if m_type == "gauge":
        c = old_instance or Gauge(metric_name, help, labelnames=label_keys, registry=registry)
        c.labels(**labels).set(value)
    elif m_type == "counter":
        c = old_instance or Counter(metric_name, help, labelnames=label_keys, registry=registry)
        c.labels(**labels).inc(value)
    elif m_type == "info":
        c = old_instance or Info(metric_name, help, labelnames=label_keys, registry=registry)
        c.labels(**labels).info(value)
    else:
        raise ValueError("metric type {m_type} not supported")

    return c


if output["scrapeable"]:
    # Add prometheus wsgi middleware to route /metrics requests
    app.wsgi_app = DispatcherMiddleware(
        app.wsgi_app, {"/metrics": make_wsgi_app(registry=registry)}
    )


@app.route(f"{webhook_basepath}/<event_title>", methods=["POST", "PUT", "DELETE"])
def receive_webhook_request(event_title):
    """receive a webhook request with data"""

    metric_name = event_title

    if request.method == "DELETE":

        if metric_name not in metrics_cache:
            return Response(
                json.dumps({"error": "not found"}), status=404, mimetype="application/json"
            )

        metrics_cache.pop(metric_name)
        # TODO refactor to separate outputs function
        if output["textfile"]:
            write_to_textfile(
                f"{textfile_dir}/webhook_metrics.prom",
                registry,
            )
        return Response(
            json.dumps({"removed_metric": metric_name}), status=202, mimetype="application/json"
        )

    data = request.json

    handler = next(
        (c for c in config["event_handlers"] if re.match(c["event_title"], event_title)),
        None,
    )

    # no matching event handler found, return
    if not handler:
        return Response(
            json.dumps(
                {
                    "error": "not found",
                    "webhook_basepath": config["webhook_basepath"],
                    "configured_events": [
                        f'{config["webhook_basepath"]}/ + /{c["event_title"]}/'
                        for c in config["event_handlers"]
                    ],
                }
            ),
            status=404,
            mimetype="application/json",
        )

    metric_extractors = handler.get("extractors", {})

    jmespath_data = {"data": data, "req": vars(request)}

    help = run_extractor(metric_extractors.get("help", None), jmespath_data, "default help")
    m_type = run_extractor(metric_extractors.get("type", None), jmespath_data, "gauge")
    value = float(run_extractor(metric_extractors.get("value", None), jmespath_data, None))

    labels = run_extractor(
        metric_extractors.get("labels", None),
        jmespath_data,
        {"warn": "label extraction failed"},
    )

    if isinstance(labels, list):
        labels = reduce(lambda a, b: {**a, **b}, labels)

    key = f"{metric_name}"

    old_instance = None
    if key in metrics_cache:
        old_instance = metrics_cache[key]["instance"]

    metric_instance = setup_metric(
        m_type, metric_name, help, labels, value, old_instance=old_instance
    )

    metrics_cache[key] = {
        "instance": metric_instance,
        "name": metric_name,
        "help": help,
        "type": m_type,
        "value": value,
        "labels": labels,
    }

    if debug:
        print(metrics_cache)

    if output["textfile"]:
        # parameterize which output - textfile/pushgateway/scrapeable metrics path
        write_to_textfile(
            f"{textfile_dir}/webhook_metrics.prom",
            registry,
        )

    return Response("", status=200, mimetype="application/json")


@app.route("/")
def index():
    return render_template("index.html", config=config)
