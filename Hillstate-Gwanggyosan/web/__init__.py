import os
import sys
CURPATH = os.path.dirname(os.path.abspath(__file__))
sys.path.extend([CURPATH])
sys.path = list(set(sys.path))
del CURPATH
from flask import Flask
from flask_bootstrap import Bootstrap
from flask_moment import Moment
from config import Config, get_app_config

bootstrap = Bootstrap()
moment = Moment()


class IngressPrefixMiddleware:
    # Home Assistant 애드온 Ingress로 접속하면 /api/hassio_ingress/<token> 경로 아래에서 페이지가 열린다.
    # Supervisor가 넘겨주는 X-Ingress-Path 헤더를 SCRIPT_NAME으로 지정해 url_for / request.script_root가
    # 올바른 경로를 만들도록 한다 (직접 포트 접속 시에는 헤더가 없으므로 기존과 동일하게 동작)
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        prefix = environ.get('HTTP_X_INGRESS_PATH', '')
        if prefix:
            environ['SCRIPT_NAME'] = prefix.rstrip('/')
        return self.wsgi_app(environ, start_response)


def create_webapp():
    app = Flask(__name__)
    app.wsgi_app = IngressPrefixMiddleware(app.wsgi_app)

    app_config: Config = get_app_config()
    app.config.from_object(app_config)

    app_config.init_app(app)
    bootstrap.init_app(app)
    moment.init_app(app)

    from .main import main as blueprint_main
    app.register_blueprint(blueprint_main)

    from .api import api as blueprint_api
    app.register_blueprint(blueprint_api, url_prefix='/api')

    print(f"Flask App is created ({app})")

    return app
