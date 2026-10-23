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

## Setup on Unraid

There's no prebuilt image, so Unraid builds it from this repo using the
**Docker Compose Manager** plugin.

### 1. Install the Compose plugin

1. Open the **Apps** tab. If Unraid asks you to install Community Applications, do it.
2. Search for **Docker Compose Manager** and click **Install**.

### 2. Get the code onto the server

1. In the Unraid web UI, click the **Terminal** icon (`>_`) in the top-right corner.
2. Run:
   ```sh
   cd /mnt/user/appdata
   git clone https://github.com/cabledogs/youtube_live
   ```
   If `git` isn't found, download it instead:
   ```sh
   cd /mnt/user/appdata
   wget -O youtube_live.tar.gz https://github.com/cabledogs/youtube_live/archive/refs/heads/main.tar.gz
   tar xzf youtube_live.tar.gz && mv youtube_live-main youtube_live && rm youtube_live.tar.gz
   ```
   (If the repo is private, download the ZIP from GitHub on your PC instead,
   then copy the folder to `/mnt/user/appdata/youtube_live` over the network share.)

### 3. Edit the config

1. Find your Unraid server's IP: **Settings → Network Settings** (e.g. `192.168.1.50`).
2. Open the config:
   ```sh
   nano /mnt/user/appdata/youtube_live/config.yaml
   ```
3. Set `base_url: http://192.168.1.50:5004` (your IP), and replace the example
   channels with yours.
4. Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

### 4. Create the stack

1. Go to the **Docker** tab, scroll down to **Compose**, and click **Add New Stack**.
2. **Stack Name:** `youtube_live`. **Description:** optional, e.g. `YouTube channels as Plex Live TV`.
3. Under **Compose Sources & Files**, choose **External folder**. A path box appears;
   enter `/mnt/user/appdata/youtube_live` (the folder from step 2, which already
   contains `docker-compose.yml`).
4. Leave everything else at its default:
   - **External ENV File Path:** `/mnt/user/appdata/youtube_live/.env`
     (an empty placeholder in the repo; the app doesn't use it, but the plugin may
     require it before **Create** works)
   - **Compose File Discovery:** leave as is
   - **Override File Management:** **Automatic**
5. Click **Create**.
6. Find `youtube_live` in the Compose list and click **Compose Up** (or the
   gear/stack menu → **Compose Up**). The first run builds the image, which takes a few
   minutes. Wait until it finishes.
7. Turn **Autostart** on for the stack, so it starts with the array.

> Don't use the **Git repository** option. The plugin keeps its own hidden copy of
> the code, which makes `config.yaml` hard to find and edit.

### 5. Check it's working

1. Open `http://192.168.1.50:5004/lineup.json` in your browser. You should see your channels.
2. Open `http://192.168.1.50:5004/xmltv.xml`. You should see video titles in the guide
   (the first scan takes a minute or two per channel).
3. To see logs: on the Docker tab, click the `yt-live-tv` container icon → **Logs**.

### 6. Add it to Plex

1. In Plex, go to **Settings → Live TV & DVR → Set up Plex DVR**.
2. If it doesn't find the tuner, click **"Don't see your HDHomeRun device?"** and enter
   `192.168.1.50:5004`.
3. Click **Continue**. When asked for the guide, click
   **"Have an XMLTV guide on your server? Click here"** and enter
   `http://192.168.1.50:5004/xmltv.xml`.
4. Confirm the channel matches and click **Continue**. Your channels will now show
   in Plex under **Live TV**, with the guide showing each video's title.

### Changing channels later

1. Edit `/mnt/user/appdata/youtube_live/config.yaml`.
2. On the Docker tab, restart the `yt-live-tv` container.
3. In Plex, open **Settings → Live TV & DVR**, click the device's settings,
   **Scan channels**, then **Refresh guide**.

### Updating the code

```sh
cd /mnt/user/appdata/youtube_live && git pull
```
Then on the Docker tab → Compose → `youtube_live` → **Compose Down**, then
**Compose Up**. If the stack menu has **Update Stack** or **Rebuild**, use that so the
image is rebuilt with the new code. Otherwise run this in the terminal:
`cd /mnt/user/appdata/youtube_live && docker compose up -d --build`.

### Unraid troubleshooting

- **Port 5004 already used:** change the left side of the port (e.g. `"5005:5004"`)
  and use `:5005` in `base_url` and in Plex.
- **Plex can't reach the tuner:** use the server's LAN IP, not `localhost`.
  This works with Plex on either host or bridge networking.
- **Channels show no videos:** check the logs. "Sign in to confirm you're not a bot"
  means you need a `cookies.txt`. Put it in `/mnt/user/appdata/youtube_live/`,
  uncomment the `cookies.txt` line in `/mnt/user/appdata/youtube_live/docker-compose.yml`, set `cookies: /config/cookies.txt` in
  `config.yaml`, and run Compose Up again.

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
