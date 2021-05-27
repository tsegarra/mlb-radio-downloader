import requests
import subprocess
import dateutil.parser
import os
import re
import lxml
import lxml, lxml.etree
import random
import string
from itertools import chain
from orderedattrdict import AttrDict
import pytz
from six import StringIO
from datetime import datetime, timedelta

session = requests.Session()

SCHEDULE_TEMPLATE = (
    "http://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&startDate={start}&endDate={end}"
    "&gameType={game_type}&gamePk={game_id}"
    "&teamId={team_id}"
    "&hydrate=linescore,team,game(content(summary,media(epg)),tickets)"
)
def session_teams(season):
  teams_url = (
      "http://statsapi.mlb.com/api/v1/teams"
      "?sportId={sport}&{season}".format(
          sport=1,
          season=season if season else ""
      )
  )
  return AttrDict(
      (team["abbreviation"].lower(), team["id"])
      for team in sorted(session.get(teams_url).json()["teams"],
                         key=lambda t: t["fileCode"])
  )

game_date = '2021-05-25' # TODO dynamic
game_date = dateutil.parser.parse(game_date)
game_number = 1

teams = session_teams(season=game_date.year)

team = 'nym' # TODO dynamic
team_id = teams.get(team)

start = game_date
end = game_date

game_type = ""
game_id = ""

url = SCHEDULE_TEMPLATE.format(
    start = start.strftime("%Y-%m-%d") if start else "",
    end = end.strftime("%Y-%m-%d") if end else "",
    game_type = game_type if game_type else "",
    team_id = team_id if team_id else "",
    game_id = game_id if game_id else ""
)
schedule = session.get(url).json()

game = schedule["dates"][0]["games"][0]
game_id = game["gamePk"]
epgs = game["content"]["media"]["epg"]

for epg in epgs:
  if epg['title'] == 'Audio':
    for audioStream in epg['items']:
      # you can filter by audioStream['mediaFeedSubType'] = team_id
      if audioStream['callLetters'] == 'WCBS 880':
        stream = audioStream
# stream has: id, contentId, mediaId

media_id = stream['mediaId']

AUTHN_URL = 'https://ids.mlb.com/api/v1/authn'
AUTHN_PARAMS = {
  'username': username,
  'password': password,
  'options': {
    'multiOptionalFactorEnroll': False,
    'warnBeforePasswordExpired': True,
  },
}

MLB_OKTA_URL = 'https://www.mlbstatic.com/mlb.com/vendor/mlb-okta/mlb-okta.js'
AUTHZ_URL = "https://ids.mlb.com/oauth2/aus1m088yK07noBfh356/v1/authorize"


authn_response = session.post(AUTHN_URL, json=AUTHN_PARAMS).json()
session_token = authn_response['sessionToken']

content = session.get(MLB_OKTA_URL).text
OKTA_CLIENT_ID_RE = re.compile("""production:{clientId:"([^"]+)",""")
okta_client_id = OKTA_CLIENT_ID_RE.search(content).groups()[0]

# ----------------------------------------------------------------------
# Okta authentication -- used to get media entitlement later
# ----------------------------------------------------------------------
def gen_random_string(n):
    return ''.join(
        random.choice(
            string.ascii_uppercase + string.digits
        ) for _ in range(64)
    )

STATE = gen_random_string(64)
NONCE = gen_random_string(64)

AUTHZ_PARAMS = {
    "client_id": okta_client_id,
    "redirect_uri": "https://www.mlb.com/login",
    "response_type": "id_token token",
    "response_mode": "okta_post_message",
    "state": STATE,
    "nonce": NONCE,
    "prompt": "none",
    "sessionToken": session_token,
    "scope": "openid email"
}
authz_response = session.get(AUTHZ_URL, params=AUTHZ_PARAMS)
authz_content = authz_response.text

for line in authz_content.split("\n"):
    if "data.access_token" in line:
        OKTA_ACCESS_TOKEN = line.split("'")[1].encode('utf-8').decode('unicode_escape')
        break
else:
    raise Exception(authz_content)

# ----------------------------------------------------------------------
# Get device assertion - used to get device token
# ----------------------------------------------------------------------
MLB_API_KEY_URL = "https://www.mlb.com/tv/g490865/"
content = session.get(MLB_API_KEY_URL).text
parser = lxml.etree.HTMLParser()
data = lxml.etree.parse(StringIO(content), parser)

API_KEY_RE = re.compile(r'"x-api-key","value":"([^"]+)"')
CLIENT_API_KEY_RE = re.compile(r'"clientApiKey":"([^"]+)"')
scripts = data.xpath(".//script")
for script in scripts:
    if script.text and "x-api-key" in script.text:
        api_key = API_KEY_RE.search(script.text).groups()[0]
    if script.text and "clientApiKey" in script.text:
        client_api_key = CLIENT_API_KEY_RE.search(script.text).groups()[0]

DEVICES_HEADERS = {
    "Authorization": "Bearer %s" % (client_api_key),
    "Origin": "https://www.mlb.com",
}

DEVICES_PARAMS = {
    "applicationRuntime": "firefox",
    "attributes": {},
    "deviceFamily": "browser",
    "deviceProfile": "macosx"
}

BAM_DEVICES_URL = "https://us.edge.bamgrid.com/devices"
devices_response = session.post(
    BAM_DEVICES_URL,
    headers=DEVICES_HEADERS, json=DEVICES_PARAMS
).json()

DEVICES_ASSERTION=devices_response["assertion"]

# ----------------------------------------------------------------------
# Get device token
# ----------------------------------------------------------------------

TOKEN_PARAMS = {
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
    "latitude": "0",
    "longitude": "0",
    "platform": "browser",
    "subject_token": DEVICES_ASSERTION,
    "subject_token_type": "urn:bamtech:params:oauth:token-type:device"
}
BAM_TOKEN_URL = "https://us.edge.bamgrid.com/token"
token_response = session.post(
    BAM_TOKEN_URL, headers=DEVICES_HEADERS, data=TOKEN_PARAMS
).json()


DEVICE_ACCESS_TOKEN = token_response["access_token"]
DEVICE_REFRESH_TOKEN = token_response["refresh_token"]

# ----------------------------------------------------------------------
# Create session -- needed for device ID, which is used for entitlement
# ----------------------------------------------------------------------
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.12; rv:56.0) "
              "Gecko/20100101 Firefox/56.0.4")
BAM_SDK_VERSION = "3.4"
PLATFORM = "macintosh"
SESSION_HEADERS = {
    "Authorization": DEVICE_ACCESS_TOKEN,
    "User-agent": USER_AGENT,
    "Origin": "https://www.mlb.com",
    "Accept": "application/vnd.session-service+json; version=1",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.5",
    "x-bamsdk-version": BAM_SDK_VERSION,
    "x-bamsdk-platform": PLATFORM,
    "Content-type": "application/json",
    "TE": "Trailers"
}
BAM_SESSION_URL = "https://us.edge.bamgrid.com/session"
session_response = session.get(
    BAM_SESSION_URL,
    headers=SESSION_HEADERS
).json()
DEVICE_ID = session_response["device"]["id"]

# ----------------------------------------------------------------------
# Get entitlement token
# ----------------------------------------------------------------------
ENTITLEMENT_PARAMS={
    "os": PLATFORM,
    "did": DEVICE_ID,
    "appname": "mlbtv_web"
}

ENTITLEMENT_HEADERS = {
    "Authorization": "Bearer %s" % (OKTA_ACCESS_TOKEN),
    "Origin": "https://www.mlb.com",
    "x-api-key": api_key

}
BAM_ENTITLEMENT_URL = "https://media-entitlement.mlb.com/api/v3/jwt"
entitlement_response = session.get(
    BAM_ENTITLEMENT_URL,
    headers=ENTITLEMENT_HEADERS,
    params=ENTITLEMENT_PARAMS
)

ENTITLEMENT_TOKEN = entitlement_response.content

# ----------------------------------------------------------------------
# Finally (whew!) get access token using entitlement token
# ----------------------------------------------------------------------
headers = {
    "Authorization": "Bearer %s" % (client_api_key),
    "User-agent": USER_AGENT,
    "Accept": "application/vnd.media-service+json; version=1",
    "x-bamsdk-version": BAM_SDK_VERSION,
    "x-bamsdk-platform": PLATFORM,
    "origin": "https://www.mlb.com"
}
data = {
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
    "platform": "browser",
    "subject_token": ENTITLEMENT_TOKEN,
    "subject_token_type": "urn:bamtech:params:oauth:token-type:account"
}
response = session.post(
    BAM_TOKEN_URL,
    data=data,
    headers=headers
)
# from requests_toolbelt.utils import dump
# print(dump.dump_all(response).decode("utf-8"))
response.raise_for_status()
token_response = response.json()

access_token_expiry = datetime.now(tz=pytz.UTC) + \
             timedelta(seconds=token_response["expires_in"])
access_token = token_response["access_token"]

headers={
    "Authorization": access_token,
    "User-agent": USER_AGENT,
    "Accept": "application/vnd.media-service+json; version=1",
    "x-bamsdk-version": "3.0",
    "x-bamsdk-platform": PLATFORM,
    "origin": "https://www.mlb.com"
}

STREAM_URL_TEMPLATE="https://edge.svcs.mlb.com/media/{media_id}/scenarios/browser~csai"
stream_url = STREAM_URL_TEMPLATE.format(media_id=media_id)
stream = session.get(
    stream_url,
    headers=headers
).json()
media_url = stream["stream"]["complete"]

header_args = []
cookie_args = []

session.headers = {
  "Authorization": access_token
}

if session.headers:
    header_args = list(
        chain.from_iterable([
            ("--http-header", f"{k}={v}")
        for k, v in session.headers.items()
    ]))

if session.cookies:
    cookie_args = list(
        chain.from_iterable([
            ("--http-cookie", f"{c.name}={c.value}")
        for c in session.cookies
    ]))

cmd = [
    "streamlink",
] + cookie_args + header_args + [
    media_url,
    "best",
] + ["-o", "degrom.aac"]

proc = subprocess.Popen(cmd, stdout=None)
proc.wait()
