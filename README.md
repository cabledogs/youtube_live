# yt-live-tv

Turns YouTube channels into live TV channels in Plex, with **no downloading**.
Videos are pulled from YouTube on the fly (yt-dlp + ffmpeg) when you tune in.

- Shows up in Plex as an HDHomeRun tuner (Live TV & DVR)
- Any number of channels; each can combine several YouTube channels or playlists
- Full program guide: every slot shows the video's title, description and thumbnail
- Runs on a fixed repeating schedule, so the guide matches what's playing
- Checks for new uploads every few hours

## Setup

1. Edit `config.yaml`:
   - set `base_url` to this server's LAN IP, e.g. `http://192.168.1.50:5004`
   - add your channels (`number`, `name`, `url` or `urls`)
2. Start it:
   ```sh
   docker compose up -d --build
   docker compose logs -f     # wait for "[Channel] N videos" lines
   ```
3. In Plex: **Settings → Live TV & DVR → Set up Plex DVR**
   - If the tuner isn't found automatically, enter `192.168.1.50:5004`
   - When it asks for a guide, choose **"Have an XMLTV guide on your server? Click here"**
     and enter `http://192.168.1.50:5004/xmltv.xml`
   - Match channels and finish
4. After adding or changing channels: `docker compose restart`, then in Plex
   go to Live TV & DVR → the tuner's settings → **Scan channels**, and **Refresh guide**.

## URLs

| URL | What it is |
| --- | --- |
| `/discover.json`, `/lineup.json` | HDHomeRun emulation for Plex/Jellyfin/Emby |
| `/xmltv.xml` | Program guide |
| `/playlist.m3u` | Playlist for VLC, TiviMate and other IPTV apps |
| `/stream/<number>` | The live stream of one channel |

## Notes

- Changing a channel restarts the video airing at that moment, in the middle, like real TV.
- Shuffled channels keep a stable order. When new uploads arrive, the schedule
  reshuffles and Plex picks up the new guide on its next guide refresh.
- Default quality is 720p H.264, which Plex plays without transcoding. For 1080p,
  set `format: "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080]"`.
- If the logs say YouTube wants you to "sign in to confirm you're not a bot",
  export a `cookies.txt` from your browser, mount it in `docker-compose.yml`
  and set `cookies: /config/cookies.txt`.
- yt-dlp updates itself each time the container starts. If streams break after a
  YouTube change, run `docker compose restart`.
- Each viewer uses one YouTube stream; nothing is cached on disk.
