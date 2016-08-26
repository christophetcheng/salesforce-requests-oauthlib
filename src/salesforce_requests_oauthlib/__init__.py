# TODO: saved refresh tokens may not play well with multiple clients running
#       at once
import os.path
import BaseHTTPServer
import thread
from executor import execute
import webbrowser
import ssl
import pickle
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError
from oauthlib.oauth2.rfc6749.clients import LegacyApplicationClient

default_settings_path = \
    os.path.expanduser('~/.salesforce_requests_oauthlib')

default_refresh_token_filename = 'refresh_tokens.pickle'

base_url_template = \
    'https://{{0}}.salesforce.com/services/oauth2/{0}'

authorization_url_template = base_url_template.format(
    'authorize'
)

token_url_template = base_url_template.format(
    'token'
)


class RequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.oauth2_full_path = 'https://{0}:{1}{2}'.format(
            self.server.server_name,
            str(self.server.server_port),
            self.path
        )
        self.send_response(200, 'OK')
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

        def shutdown_server(server):
            server.shutdown()

        thread.start_new_thread(shutdown_server, (self.server,))


class SalesforceOAuth2Session(OAuth2Session):
    @staticmethod
    def generate_local_webserver_key(file_basename='server',
                                     settings_path=None):
        if settings_path is None:
            settings_path = default_settings_path

        command = 'mkdir -p {0} && openssl req -nodes -new -x509 ' \
                  '-keyout {1} ' \
                  '-out {2} ' \
                  '-subj "/C=/ST=/L=/O=/OU=/CN=localhost"'.format(
                      settings_path,
                      os.path.join(settings_path, 'server.key'),
                      os.path.join(settings_path, 'server.cert')
                  )
        execute(command)

    def __init__(self, client_id, client_secret, username,
                 settings_path=None,
                 sandbox=False,
                 local_server_settings=('localhost', 60443),
                 password=None,
                 ignore_cached_refresh_tokens=False,
                 version=None):

        self.client_secret = client_secret
        self.username = username
        self.password = password
        self.local_server_settings = local_server_settings
        self.token_url = token_url_template.format(
            'test' if sandbox else 'login'
        )
        # Avoid name collision
        self.authorization_url_location = authorization_url_template.format(
            'test' if sandbox else 'login'
        )

        self.callback_url = 'https://{0}:{1}'.format(
            self.local_server_settings[0],
            str(self.local_server_settings[1])
        )

        # Side effect here is to set self.client_id
        super(SalesforceOAuth2Session, self).__init__(
            client_id=client_id,
            redirect_uri=self.callback_url,
            client=LegacyApplicationClient(
                client_id=client_id
            ) if password is not None else None
        )

        if settings_path is None:
            settings_path = default_settings_path
        self.settings_path = settings_path

        self.refresh_token_filename = os.path.join(
            self.settings_path,
            default_refresh_token_filename
        )

        refresh_token = None

        if not ignore_cached_refresh_tokens:
            try:
                with open(self.refresh_token_filename) as fileh:
                    saved_refresh_tokens = pickle.load(fileh)
                    if self.username in saved_refresh_tokens:
                        refresh_token = saved_refresh_tokens[self.username]
            except IOError:
                pass

        if refresh_token is None:
            if self.password is None:
                self.launch_webbrowser_flow()
            else:
                self.launch_password_flow()
        else:
            self.token = {
                'token_type': 'Bearer',
                'refresh_token': refresh_token,
                'access_token': 'Would you eat them in a box?'
            }

            try:
                self.refresh_token(
                    self.token_url,
                    client_id=self.client_id,
                    client_secret=self.client_secret
                )
            except InvalidGrantError:
                if self.password is None:
                    self.launch_webbrowser_flow()
                else:
                    self.launch_password_flow()

        self.version = version
        if self.version is None:
            self.use_latest_version()

    def use_latest_version(self):
        self.version = self.get('/services/data/').json()[-1]['version']

    def launch_webbrowser_flow(self):
        webbrowser.open(
            self.authorization_url(
                self.authorization_url_location
            )[0],
            new=2,
            autoraise=True
        )

        httpd = BaseHTTPServer.HTTPServer(
            self.local_server_settings,
            RequestHandler
        )

        httpd.timeout = 30

        httpd.socket = ssl.wrap_socket(
            httpd.socket,
            keyfile=os.path.join(self.settings_path, 'server.key'),
            certfile=os.path.join(self.settings_path, 'server.cert'),
            server_side=True
        )

        httpd.serve_forever()
        httpd.server_close()

        self.fetch_token(
            token_url=self.token_url,
            authorization_response=httpd.oauth2_full_path,
            client_id=self.client_id,
            client_secret=self.client_secret
        )

        saved_refresh_tokens = {}
        try:
            with open(self.refresh_token_filename, 'r') as fileh:
                saved_refresh_tokens = pickle.load(fileh)
        except IOError:
            pass

        saved_refresh_tokens[self.username] = self.token['refresh_token']

        with open(self.refresh_token_filename, 'w') as fileh:  # Yes, overwrite
            pickle.dump(saved_refresh_tokens, fileh)

    def launch_password_flow(self):
        self.fetch_token(
            token_url=self.token_url,
            client_id=self.client_id,
            client_secret=self.client_secret,
            username=self.username,
            password=self.password
        )

    def request(self, *args, **kwargs):
        version_substitution = True
        if 'version_substitution' in kwargs:
            version_substitution = kwargs['version_substitution']

        # Not checking the first two args for sanity - seems like overkill.
        url = args[1]

        if version_substitution:
            url = url.replace('vXX.X', 'v{0}'.format(
                str(self.version))
                    if hasattr(self, 'version') and self.version is not None
                    else ''
            )

        if 'instance_url' in self.token and url.startswith('/'):
            # Then it's relative
            # We append the instance_url for convenience
            url = '{0}{1}'.format(
                self.token['instance_url'],
                url
            )

        return super(SalesforceOAuth2Session, self).request(
            args[0],
            url,
            *args[2:],
            **kwargs
        )
