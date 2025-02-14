import os
import mimetypes
import json
from datetime import datetime
from string import Template
from owrx.websocket import WebSocketConnection
from owrx.config import PropertyManager
from owrx.source import ClientRegistry
from owrx.connection import WebSocketMessageHandler
from owrx.version import openwebrx_version
from owrx.feature import FeatureDetector

import logging
logger = logging.getLogger(__name__)

class Controller(object):
    def __init__(self, handler, request):
        self.handler = handler
        self.request = request
    def send_response(self, content, code = 200, content_type = "text/html", last_modified: datetime = None, max_age = None):
        self.handler.send_response(code)
        if content_type is not None:
            self.handler.send_header("Content-Type", content_type)
        if last_modified is not None:
            self.handler.send_header("Last-Modified", last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        if max_age is not None:
            self.handler.send_header("Cache-Control", "max-age: {0}".format(max_age))
        self.handler.end_headers()
        if (type(content) == str):
            content = content.encode()
        self.handler.wfile.write(content)


class StatusController(Controller):
    def handle_request(self):
        pm = PropertyManager.getSharedInstance()
        # TODO keys that have been left out since they are no longer simple strings: sdr_hw, bands, antenna
        vars = {
            "status": "active",
            "name": pm["receiver_name"],
            "op_email": pm["receiver_admin"],
            "users": ClientRegistry.getSharedInstance().clientCount(),
            "users_max": pm["max_clients"],
            "gps": pm["receiver_gps"],
            "asl": pm["receiver_asl"],
            "loc": pm["receiver_location"],
            "sw_version": openwebrx_version,
            "avatar_ctime": os.path.getctime("htdocs/gfx/openwebrx-avatar.png")
        }
        self.send_response("\n".join(["{key}={value}".format(key = key, value = value) for key, value in vars.items()]))

class AssetsController(Controller):
    def serve_file(self, file, content_type = None):
        try:
            modified = datetime.fromtimestamp(os.path.getmtime('htdocs/' + file))

            if "If-Modified-Since" in self.handler.headers:
                client_modified = datetime.strptime(self.handler.headers["If-Modified-Since"], "%a, %d %b %Y %H:%M:%S %Z")
                if modified <= client_modified:
                    self.send_response("", code = 304)
                    return

            f = open('htdocs/' + file, 'rb')
            data = f.read()
            f.close()

            if content_type is None:
                (content_type, encoding) = mimetypes.MimeTypes().guess_type(file)
            self.send_response(data, content_type = content_type, last_modified = modified, max_age = 3600)
        except FileNotFoundError:
            self.send_response("file not found", code = 404)
    def handle_request(self):
        filename = self.request.matches.group(1)
        self.serve_file(filename)

class TemplateController(Controller):
    def render_template(self, file, **vars):
        f = open('htdocs/' + file, 'r')
        template = Template(f.read())
        f.close()

        return template.safe_substitute(**vars)

    def serve_template(self, file, **vars):
        self.send_response(self.render_template(file, **vars), content_type = 'text/html')

    def default_variables(self):
        return {}


class WebpageController(TemplateController):
    def template_variables(self):
        header = self.render_template('include/header.include.html')
        return { "header": header }


class IndexController(WebpageController):
    def handle_request(self):
        self.serve_template("index.html", **self.template_variables())


class MapController(WebpageController):
    def handle_request(self):
        #TODO check if we have a google maps api key first?
        self.serve_template("map.html", **self.template_variables())

class FeatureController(WebpageController):
    def handle_request(self):
        self.serve_template("features.html", **self.template_variables())

class ApiController(Controller):
    def handle_request(self):
        data = json.dumps(FeatureDetector().feature_report())
        self.send_response(data, content_type = "application/json")

class WebSocketController(Controller):
    def handle_request(self):
        conn = WebSocketConnection(self.handler, WebSocketMessageHandler())
        # enter read loop
        conn.read_loop()
