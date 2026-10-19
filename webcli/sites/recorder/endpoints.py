"""Every URL webcli recorder touches. Fix this file first when a capture disagrees.

recorder.google.com loads its data over gRPC-web JSON from pixelrecorder-pa.clients6.google.com:
  POST /$rpc/java.com.google.wireless.android.pixel.recorder.protos.PlaybackService/<Method>
GetRecordingList answers [[recording, ...], cursor] when the app opens; GetTranscription answers a recording's
transcript when its page (recorder.google.com/<audio_id>) opens. webcli recorder reads those answers (INTERCEPT) and
never calls them itself. Audio: GET usercontent.recorder.google.com/download/playback/<audio_id> with the profile's
cookies (page.request, which the route guard never sees: guard.audio_url_allowed checks every hop).

The guard allows exactly READ_METHODS (captured live, 2026-10-19). The app asks for ListLabels before
GetRecordingList, and a recording's page asks for GetRecordingInfo before GetTranscription: with either blocked the
data call never comes. The page's other calls stay blocked; nothing webcli reads waits on them: GetShareList,
GetAudioTag, GetWaveform, GetGlobalSearchReadiness, peoplestack-pa's contact autocomplete, /jserror and
/api/monitoring, telemetry. Run `python -m webcli.core.capture recorder` to see what the guard blocked, and add a
method only once you know it is a read.
"""

from __future__ import annotations

import re

APP_HOST = "recorder.google.com"
APP_URL = "https://recorder.google.com"
RECORDING_URL = APP_URL + "/{audio_id}"
RPC_HOST = "pixelrecorder-pa.clients6.google.com"
RPC_PREFIX = "/$rpc/java.com.google.wireless.android.pixel.recorder.protos.PlaybackService/"
READ_METHODS = frozenset({"ListLabels", "GetRecordingList", "GetRecordingInfo", "GetTranscription"})
AUDIO_HOST = "usercontent.recorder.google.com"
AUDIO_URL = f"https://{AUDIO_HOST}/download/playback/{{audio_id}}"
AUDIO_HOSTS = re.compile(r"usercontent\.recorder\.google\.com|[a-z0-9-]+\.googleusercontent\.com")
HOSTS = (APP_HOST, AUDIO_HOST)
RPC_SECONDS = 30  # how long an opened page gets to send its RPC
