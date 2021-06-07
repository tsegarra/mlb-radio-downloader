import sys
import re
import random
import string
import requests
import subprocess
import pytz
import dateutil.parser
import lxml, lxml.etree
from getpass import getpass
from itertools import chain
from six import StringIO
from datetime import datetime, timedelta

class MlbApiUtil():
  def __init__(self, game_date, team_abbr):
    self.session = requests.Session()
    self.game_date = game_date
    self.team = self._get_team_id(team_abbr)
    self.game = self._get_game_info()
    self.streams = self._get_all_streams()

  def _get_team_id(self, abbr):
    teams_url = ("http://statsapi.mlb.com/api/v1/teams"
        "?sportId=1&" + str(self.game_date.year))
    teams = self.session.get(teams_url).json()["teams"]
    for team in teams:
      if team["abbreviation"].lower() == abbr.lower():
        return team["id"]
    print("The team abbreviation you provided is not valid.")
    print("Please use one from the list below:")
    print(", ".join([team["abbreviation"] for team in teams]))
    sys.exit(1)

  def _get_game_info(self):
    url = (
      "http://statsapi.mlb.com/api/v1/schedule"
      "?sportId=1&startDate={date}&endDate={date}"
      "&teamId={team_id}"
      "&hydrate=linescore,team,game(content(summary,media(epg)),tickets)"
    ).format(
      date = self.game_date.strftime("%Y-%m-%d"),
      team_id = self.team,
    )
    schedule = self.session.get(url).json()
    return schedule["dates"][0]["games"][0]

  def _get_all_streams(self):
    try:
      streams = []
      for epg in self.game["content"]["media"]["epg"]:
        if epg['title'] == 'Audio':
          for audioStream in epg['items']:
            # Note: audioStream['mediaFeedSubType'] = team_id
            streams.append({
              'callLetters': audioStream['callLetters'],
              'mediaId': audioStream['mediaId']
            })
      return streams
    except KeyError:
      print("Audio for the game you requested is not available.")
      sys.exit(1)

  def _random_string(self, n):
    return ''.join(
      random.choice(
        string.ascii_uppercase + string.digits
      ) for _ in range(n)
    )

  def _get_credentials_from_user(self):
    print("Enter your MLB.TV username:")
    self.username = input()
    self.password = getpass()

  def _get_session_token(self):
    self._get_credentials_from_user()
    response = self.session.post("https://ids.mlb.com/api/v1/authn",
      json={
        "username": self.username,
        "password": self.password,
      }).json()
    self.session_token = response["sessionToken"]

  def _get_access_token(self):
    self._get_session_token()

    okta_url = 'https://www.mlbstatic.com/mlb.com/vendor/mlb-okta/mlb-okta.js'
    mlb_auth_url = "https://ids.mlb.com/oauth2/aus1m088yK07noBfh356/v1/authorize"

    content = self.session.get(okta_url).text
    okta_client_id_re = re.compile("""production:{clientId:"([^"]+)",""")
    okta_client_id = okta_client_id_re.search(content).groups()[0]

    authz_response = self.session.get(mlb_auth_url, params = {
      "client_id": okta_client_id,
      "redirect_uri": "https://www.mlb.com/login",
      "response_type": "id_token token",
      "response_mode": "okta_post_message",
      "state": self._random_string(64),
      "nonce": self._random_string(64),
      "prompt": "none",
      "sessionToken": self.session_token,
      "scope": "openid email"
    })
    authz_content = authz_response.text

    for line in authz_content.split("\n"):
      if "data.access_token" in line:
        okta_access_token = line.split("'")[1].encode('utf-8').decode('unicode_escape')
        break
    else:
      raise Exception(authz_content)

    content = self.session.get("https://www.mlb.com/tv/g490865/").text
    parser = lxml.etree.HTMLParser()
    data = lxml.etree.parse(StringIO(content), parser)

    x_api_key_re = re.compile(r'"x-api-key","value":"([^"]+)"')
    client_api_key_re = re.compile(r'"clientApiKey":"([^"]+)"')
    scripts = data.xpath(".//script")
    for script in scripts:
      if script.text and "x-api-key" in script.text:
        api_key = x_api_key_re.search(script.text).groups()[0]
      if script.text and "clientApiKey" in script.text:
        client_api_key = client_api_key_re.search(script.text).groups()[0]

    devices_headers = {
        "Authorization": "Bearer %s" % (client_api_key),
        "Origin": "https://www.mlb.com",
    }

    devices_response = self.session.post(
        "https://us.edge.bamgrid.com/devices",
        headers=devices_headers, json = {
          "applicationRuntime": "firefox",
          "attributes": {},
          "deviceFamily": "browser",
          "deviceProfile": "macosx"
        }
    ).json()

    bam_token_url = "https://us.edge.bamgrid.com/token"
    token_response = self.session.post(
      bam_token_url, headers=devices_headers, data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "latitude": "0",
        "longitude": "0",
        "platform": "browser",
        "subject_token": devices_response["assertion"],
        "subject_token_type": "urn:bamtech:params:oauth:token-type:device"
      }
    ).json()

    device_access_token = token_response["access_token"]

    session_response = self.session.get(
        "https://us.edge.bamgrid.com/session",
        headers={
          "Authorization": device_access_token,
          "Origin": "https://www.mlb.com",
          "Accept": "application/vnd.session-service+json; version=1",
          "Accept-Encoding": "gzip, deflate, br",
          "Accept-Language": "en-US,en;q=0.5",
          "x-bamsdk-version": "3.4",
          "Content-type": "application/json",
          "TE": "Trailers"
        }
    ).json()
     
    entitlement_response = self.session.get(
        "https://media-entitlement.mlb.com/api/v3/jwt",
        headers={
          "Authorization": "Bearer %s" % (okta_access_token),
          "Origin": "https://www.mlb.com",
          "x-api-key": api_key
        },
        params={
          "os": '',
          "did": session_response["device"]["id"],
          "appname": "mlbtv_web"
        }
    )

    response = self.session.post(
      bam_token_url,
      data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "platform": "browser",
        "subject_token": entitlement_response.content,
        "subject_token_type": "urn:bamtech:params:oauth:token-type:account"
      },
      headers={
        "Authorization": "Bearer %s" % (client_api_key),
        "Accept": "application/vnd.media-service+json; version=1",
        "x-bamsdk-version": "3.4",
        "origin": "https://www.mlb.com"
      }
    )
    response.raise_for_status()
    token_response = response.json()

    access_token_expiry = datetime.now(tz=pytz.UTC) + \
      timedelta(seconds=token_response["expires_in"])
    self.access_token = token_response["access_token"]

  def _run_streamlink(self, media_url, outfile_name):
    header_args = []
    cookie_args = []

    self.session.headers = { "Authorization": self.access_token }

    header_args = list(
      chain.from_iterable([
        ("--http-header", f"{k}={v}")
      for k, v in self.session.headers.items()
    ]))

    if self.session.cookies:
      cookie_args = list(
        chain.from_iterable([
          ("--http-cookie", f"{c.name}={c.value}")
        for c in self.session.cookies
      ]))

    cmd = [
      "streamlink",
    ] + cookie_args + header_args + [
      media_url,
      "best",
    ] + ["-o", outfile_name]

    proc = subprocess.Popen(cmd)
    proc.wait()

  def download_stream(self):
    self._get_access_token()

    stream = self.session.get(
        "https://edge.svcs.mlb.com/media/" + \
          str(self.media_id) + "/scenarios/browser~csai",
        headers={
          "Authorization": self.access_token,
          "Accept": "application/vnd.media-service+json; version=1",
        }
    ).json()
    media_url = stream["stream"]["complete"]

    outfile_name = "mlbaudio." + self.game_date.strftime("%Y-%m-%d") + ".aac"
    self._run_streamlink(media_url, outfile_name)

class MlbDownloaderUi():
  def __init__(self):
    self._validate_cli_args()

  def _validate_cli_args(self):
    USAGE = "USAGE: python3 " + sys.argv[0] + " <YYYY-MM-DD> <team_abbreviation>"
    if len(sys.argv) < 3:
      print(USAGE)
      sys.exit(1)
    try:
      self.game_date = dateutil.parser.parse(sys.argv[1])
    except TypeError:
      print("Invalid date provided.")
      print(USAGE)
      sys.exit(1)
    self.desired_team_abbr = sys.argv[2]

  def choose_stream(self, streams):
    i = 0
    for stream in streams:
      print("[" + str(i) + "] " + stream["callLetters"])
      i += 1
    stream_index_chosen = False
    while not stream_index_chosen:
      try:
        print("\nSelect a stream from the list above.")
        chosen_stream_index = int(input())
        if chosen_stream_index >= 0 and chosen_stream_index < i:
          stream_index_chosen = True
        else:
          print("Invalid index.")
      except ValueError:
        print("Invalid index; please enter an integer.")
    return streams[chosen_stream_index]["mediaId"]

def main():
  ui = MlbDownloaderUi()
  mlbapiutil = MlbApiUtil(ui.game_date, ui.desired_team_abbr)
  mlbapiutil.media_id = ui.choose_stream(mlbapiutil.streams)
  mlbapiutil.download_stream()

if __name__ == "__main__":
  main()
