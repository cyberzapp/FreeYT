# FreeYT

A small website that runs on your Windows PC. It accepts YouTube links, shows download progress, and saves videos to a folder you choose. Use it only for videos you own or are authorized to save, and follow YouTube's terms and applicable copyright rules.

## Start the website

1. Install Python 3.10 or newer for Windows if it is not already installed. During setup, enable the option to add Python to PATH.
2. Double-click `start.bat`. On first launch it creates a private environment in this folder and installs `yt-dlp`; an internet connection is needed once for setup.
3. The website opens at `http://127.0.0.1:8765`. Keep the console window open while using it. Do not open `index.html` directly; it needs the local server started by `start.bat`.
4. Paste a YouTube video link, choose a quality and save folder, then select **Download video**. An example link is prefilled and the default folder is your system Downloads folder.
5. Close the console window to stop the website.

## Convert and enhance local videos

1. Expand **Convert or enhance a video on this PC**.
2. Select or drop a `.webm` or `.mp4` file, choose a workflow, target resolution, filter preset, and encoding bitrate.
3. Choose an output folder and select **Process video locally**. The result is saved in that folder; the page also provides a before/after preview and a browser-copy link.

The studio supports format conversion, scaling up to 4K, sharpening, contrast and vibrance adjustments, and noise reduction. Upscaling enlarges the image with Lanczos resampling; it does not recreate detail absent from the source. Video processing runs through FFmpeg on this computer.

## Quality and FFmpeg

Install FFmpeg (including `ffprobe`) and make both executables available in PATH to enable local video processing and combine separate video and audio streams for the best download quality. Without FFmpeg, the downloader uses YouTube's available single-file formats, which may have a lower maximum resolution. Restart the app after installing FFmpeg.

## Notes

- The server listens only on this computer (`127.0.0.1`). It is not exposed to your network.
- Downloads are saved directly in the selected folder. The app creates that folder if it does not exist.
- The app handles one download at a time and does not support playlists.
- YouTube may restrict access to some videos or formats. The app does not bypass account checks or other access restrictions.
- The browser sends local video files only to the local app on this PC for processing; they are not sent to an external server. yt-dlp contacts YouTube to retrieve videos you request.
- The local video studio accepts `.webm` and `.mp4` files up to 8 GB.

## Troubleshooting

- **Python is required:** install Python 3.10 or newer, then run `start.bat` again.
- **Setup failed:** connect to the internet and run `start.bat` again.
- **Could not start the website:** another program may be using port `8765`; close it or change `PORT` in `app.py` and restart.
- **Download failed:** verify the link, internet connection, destination folder permissions, and whether the video is available to you. Install FFmpeg if the error mentions merging or FFmpeg.
