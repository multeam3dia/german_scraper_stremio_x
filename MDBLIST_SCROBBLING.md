# MDBList Playback Tracking

MDBList can be used alongside Simkl for playback tracking. This integration
reuses the existing `MDBLIST_API_KEY` environment variable; it does not
require a separate OAuth application or a second credential flow.

## Configuration

Keep the API key in Dockhand's environment settings (or the stack's private
`.env` file):

```dotenv
MDBLIST_API_KEY=your_mdblist_api_key
PLAYER_EVENT_PATH_TOKEN=your_existing_long_random_secret
```

Do not commit the API key or paste it into logs or chat. If the key is already
configured for the optional MDBList list exporter, the same key is reused.

The tokenized Stremio manifest must be installed, for example:

```
https://your-public-host/PLAYER_EVENT_PATH_TOKEN/manifest.json
```

The addon advertises player events when either Simkl or MDBList is configured.
Stremio must also have **Share playback events** enabled where that setting is
available. Recreate the container after changing environment variables. If
Stremio still uses an older manifest, reinstall the tokenized addon.

## Test the connection

Open the following URL in a browser, replacing the host and token:

```
https://your-public-host/PLAYER_EVENT_PATH_TOKEN/mdblist/status
```

A successful result has `"connected": true` and includes the API rate-limit
headers returned by MDBList. This endpoint makes one API request.

## Test playback scrobbling

1. Start a movie or series episode in Stremio.
2. Pause playback after a few minutes.
3. Open:

   ```
   https://your-public-host/PLAYER_EVENT_PATH_TOKEN/mdblist/playback
   ```

4. The paused item should appear in `items`, with its MDBList progress data.

The addon sends `start`, `pause` and `stop` events. It does not send a
periodic heartbeat. Repeated consecutive states are coalesced to avoid
unnecessary API calls. A stop at 80% or higher marks the item watched; a stop
below 80% is retained as a paused session. The same Stremio event is sent to
Simkl as well when Simkl is configured.

The status and playback test endpoints are protected by the same tokenized
path as the player-event endpoint. The API key is not returned by either
endpoint and is not written to application logs.

## Current scope and Stremio limitation

This first test phase verifies that MDBList accepts the player events and that
the server can read saved progress back through `/sync/playback`. It does not
yet create a dedicated Continue Watching catalog in Stremio.

The returned progress can be used to build such a catalog, but a normal Stremio
addon cannot command Stremio's native player to seek to an arbitrary timestamp.
Displaying a paused title and automatically restoring its exact position are
therefore separate capabilities; exact automatic seeking would require
client-side support.

## API usage

The integration makes one MDBList request for each accepted player state
(start, pause or stop). The manual status and playback endpoints each make one
additional request. There is no heartbeat. These requests share the account's
MDBList daily and short-window limits with the existing MDBList list exporter
and any other apps using the same account.
