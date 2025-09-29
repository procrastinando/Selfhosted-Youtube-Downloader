import gradio as gr
import yt_dlp
import os
import re
import subprocess
from collections import defaultdict
from groq import Groq
import groq
import random
import logging

# --- Configuration & Setup ---
# Setup robust logging to be visible in Docker
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load configuration from environment variables for Docker compatibility
GROQ_API_KEYS = [a.strip() for a in os.getenv("GROQ_API_KEYS", "").split(",") if a.strip()]
WHISPER_MODEL = os.getenv("WHISPER_MODEL_ID", "whisper-large-v3") # Default to v3 if not set
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Log the loaded configuration to verify
logging.info(f"Loaded {len(GROQ_API_KEYS)} Groq API keys.")
logging.info(f"Using Whisper Model: {WHISPER_MODEL}")


# --- Groq Transcription Functions ---
def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

def generate_srt_from_transcription(transcription, srt_path):
    with open(srt_path, "w", encoding="utf-8") as f:
        if hasattr(transcription, 'segments') and transcription.segments:
            for i, segment in enumerate(transcription.segments, 1):
                f.write(f"{i}\n")
                f.write(f"{format_time(segment.get('start', 0))} --> {format_time(segment.get('end', 0))}\n")
                f.write(f"{segment.get('text', '').strip()}\n\n")
            return True
    return False

def transcribe_with_groq(api_keys, audio_path, srt_path):
    if not api_keys:
         return {"error": "No Groq API keys found. Please set the GROQ_API_KEYS environment variable."}
    
    shuffled_keys = api_keys[:]
    random.shuffle(shuffled_keys)

    for key in shuffled_keys:
        try:
            client = Groq(api_key=key)
            with open(audio_path, "rb") as file:
                transcription = client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), file.read()),
                    model=WHISPER_MODEL,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            if generate_srt_from_transcription(transcription, srt_path):
                logging.info(f"Successfully transcribed using key starting with '{key[:7]}...'")
                return {"success": True, "srt_path": srt_path}
            else:
                return {"error": "Transcription succeeded but no segments were found."}

        except groq.RateLimitError:
            logging.warning(f"Key starting with '{key[:7]}...' hit a rate limit. Trying next key.")
            continue
        except groq.PermissionDeniedError:
            logging.warning(f"Key starting with '{key[:7]}...' is invalid or blocked. Trying next key.")
            continue
        except Exception as e:
            logging.error(f"An unexpected error occurred during transcription: {e}")
            return {"error": f"An unexpected error during transcription: {e}"}

    return {"error": "All Groq API keys failed due to rate limits or errors."}

# --- YouTube Download Functions ---
def get_video_info(url):
    ydl_opts = {'quiet': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            return {"error": str(e)}

def parse_formats(info_dict):
    if "error" in info_dict:
        return [], [], info_dict["error"]
    video_formats, audio_formats_parsed = defaultdict(list), []
    for f in info_dict.get('formats', []):
        filesize = f.get('filesize') or f.get('filesize_approx')
        if f.get('vcodec') != 'none' and f.get('acodec') == 'none' and filesize:
            resolution = f.get('height')
            if resolution:
                video_formats[resolution].append(f"{f.get('format_note', f.get('resolution'))} - {f.get('ext')} ({f.get('format_id')})")
        elif f.get('acodec') != 'none' and f.get('vcodec') == 'none' and filesize:
            abr = f.get('abr')
            if abr:
                audio_formats_parsed.append((abr, f"{abr}kbps - {f.get('ext')} ({f.get('format_id')})"))
    sorted_resolutions = sorted(video_formats.keys(), reverse=True)
    sorted_video_formats = ["No video"] + [item for res in sorted_resolutions for item in list(set(video_formats[res]))]
    audio_formats_parsed.sort(key=lambda x: x[0], reverse=True)
    sorted_audio_formats = list(dict.fromkeys([fmt[1] for fmt in audio_formats_parsed]))
    return sorted_video_formats, sorted_audio_formats, None

def download_media(url, format_selector, output_path_template, postprocessor_hooks=None):
    ydl_opts = {
        'format': format_selector, 'outtmpl': output_path_template,
        'merge_output_format': 'mp4', 'overwrites': True,
        'quiet': True, 'no_warnings': True
    }
    if postprocessor_hooks:
        ydl_opts['postprocessors'] = postprocessor_hooks
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# --- Gradio UI and Event Handlers ---
def on_next_button_click(url):
    gr.Info("Fetching video formats... Please wait.")
    info = get_video_info(url)
    video_formats, audio_formats, error = parse_formats(info)
    
    if error or not audio_formats:
        error_msg = error or "Could not retrieve valid audio formats."
        gr.Warning(error_msg)
        return {info_state: {"title": "error"}, subtitle: gr.update(value=f"Error: {error_msg}", visible=True)}

    default_video = next((v for v in video_formats if "1080p" in v), video_formats[1] if len(video_formats) > 1 else "No video")
    default_audio = next((a for a in audio_formats if "128kbps" in a), audio_formats[0])

    return {
        info_state: {"title": info.get('title', 'yt_video')},
        subtitle: gr.update(visible=True), video_dropdown: gr.update(choices=video_formats, value=default_video, visible=True),
        audio_dropdown: gr.update(choices=audio_formats, value=default_audio, visible=True),
        subtitles_checkbox: gr.update(visible=True), convert_button: gr.update(visible=True),
    }

def on_convert_button_click(url, video_format_str, audio_format_str, info_state, generate_subs):
    # Initial UI state reset
    yield {
        status_display: gr.update(value="Starting process...", visible=True),
        media_download_button: gr.update(visible=False),
        srt_download_button: gr.update(visible=False),
    }
    
    sanitized_title = re.sub(r'[\\/*?:"<>|]', "", info_state["title"])
    base_filepath = os.path.join(DOWNLOADS_DIR, sanitized_title)
    
    video_file, audio_file, srt_file_path = None, None, None

    try:
        # Step 1: Download Video (if selected)
        if video_format_str != "No video":
            yield { status_display: gr.update(value="➡️ Step 1/4: Downloading video...") }
            video_res = re.search(r'(\d+)p', video_format_str).group(1)
            video_selector = f'bestvideo[height<={video_res}]/bestvideo'
            download_media(url, video_selector, f"{base_filepath}_video.%(ext)s")
            # Find the downloaded video file (extension can vary)
            for ext in ['mp4', 'webm', 'mkv']:
                if os.path.exists(f"{base_filepath}_video.{ext}"):
                    video_file = f"{base_filepath}_video.{ext}"
                    break
        
        # Step 2: Download Audio
        yield { status_display: gr.update(value="➡️ Step 2/4: Downloading audio...") }
        audio_bitrate = int(re.search(r'(\d+)kbps', audio_format_str).group(1))
        audio_selector = f'bestaudio[abr<={audio_bitrate}]/bestaudio'
        # Download as m4a/opus for better quality before potential re-encoding
        download_media(url, audio_selector, f"{base_filepath}_audio.%(ext)s")
        for ext in ['m4a', 'webm', 'opus']:
             if os.path.exists(f"{base_filepath}_audio.{ext}"):
                    audio_file = f"{base_filepath}_audio.{ext}"
                    break
        
        # Step 3: Handle Subtitles
        transcription_audio_path = audio_file # Assume we use the original audio first
        if generate_subs:
            # Step 3a: Compress audio if needed
            if audio_bitrate > 70:
                yield { status_display: gr.update(value="➡️ Step 3/4: Compressing audio for transcription...") }
                transcription_audio_path = f"{base_filepath}_transcribe_compressed.mp3"
                subprocess.run([
                    'ffmpeg', '-i', audio_file, '-y',
                    '-vn', '-acodec', 'libmp3lame', '-b:a', '64k',
                    transcription_audio_path
                ], check=True, capture_output=True)
            
            # Step 3b: Transcribe
            yield { status_display: gr.update(value="➡️ Step 3/4: Generating subtitles with Groq API...") }
            srt_file_path = f"{base_filepath}.srt"
            result = transcribe_with_groq(GROQ_API_KEYS, transcription_audio_path, srt_file_path)

            if transcription_audio_path != audio_file: # Clean up compressed file
                os.remove(transcription_audio_path)

            if "error" in result:
                gr.Warning(result["error"])
                srt_file_path = None
        
        # Step 4: Merge and finalize
        yield { status_display: gr.update(value="➡️ Step 4/4: Finalizing files...") }
        final_media_file = None
        if video_file and audio_file:
            final_media_file = f"{base_filepath}.mp4"
            merge_cmd = ['ffmpeg', '-y', '-i', video_file, '-i', audio_file]
            if srt_file_path:
                merge_cmd.extend(['-i', srt_file_path, '-c', 'copy', '-c:s', 'mov_text'])
            else:
                merge_cmd.extend(['-c', 'copy'])
            merge_cmd.append(final_media_file)
            subprocess.run(merge_cmd, check=True, capture_output=True)
            os.remove(video_file)
            os.remove(audio_file)
        elif audio_file: # Audio-only download
            final_media_file = f"{base_filepath}.mp3"
            subprocess.run([
                'ffmpeg', '-y', '-i', audio_file, '-acodec', 'libmp3lame', '-b:a', f'{audio_bitrate}k', final_media_file
            ], check=True, capture_output=True)
            os.remove(audio_file)

        gr.Info("Process Complete!")
        yield {
            status_display: gr.update(value="✅ Process Complete!", visible=True),
            media_download_button: gr.update(value=final_media_file, visible=True),
            srt_download_button: gr.update(value=srt_file_path, visible=bool(srt_file_path))
        }

    except Exception as e:
        logging.error(f"An error occurred in the conversion process: {e}")
        gr.Error(f"An error occurred: {e}")
        # Clean up partial files
        for f in [video_file, audio_file]:
            if f and os.path.exists(f): os.remove(f)
        yield { status_display: gr.update(value=f"❌ Error: {e}", visible=True) }

# --- NEW: Restart function for backward compatibility ---
def restart_app():
    """Resets all UI components to their initial state."""
    return {
        info_state: {},
        url_input: gr.update(value=""),
        format_selection: gr.update(visible=False),
        subtitle: gr.update(visible=False),
        video_dropdown: gr.update(value=None, choices=[], visible=False),
        audio_dropdown: gr.update(value=None, choices=[], visible=False),
        subtitles_checkbox: gr.update(value=False, visible=False),
        convert_button: gr.update(visible=False),
        status_display: gr.update(value="", visible=False),
        media_download_button: gr.update(visible=False),
        srt_download_button: gr.update(visible=False),
    }

# --- Gradio Interface Definition ---
with gr.Blocks(theme=gr.themes.Soft(), title="Youtube Video Downloader", css="footer {visibility: hidden}") as demo:
    info_state = gr.State({})
    
    with gr.Row():
        gr.Markdown("# YouTube Video Downloader & Converter")
        # Changed scale to 0 to make it take minimum width
        restart_button = gr.Button("Restart", variant="stop", scale=0)

    with gr.Column():
        url_input = gr.Textbox(label="YouTube Video Link", placeholder="Enter YouTube URL here...")
        next_button = gr.Button("Next")
        
    with gr.Column(visible=False) as format_selection:
        subtitle = gr.Markdown("## Choose the format", visible=False)
        video_dropdown = gr.Dropdown(label="Video Quality", visible=False)
        audio_dropdown = gr.Dropdown(label="Audio Quality", visible=False)
        subtitles_checkbox = gr.Checkbox(label="Generate subtitles", value=False, visible=False)
        convert_button = gr.Button("Convert", visible=False)
    
    status_display = gr.Markdown(visible=False)
    
    gr.Markdown("---", visible=True)
    with gr.Row():
        media_download_button = gr.DownloadButton(label="Download Media File", visible=False)
        srt_download_button = gr.DownloadButton(label="Download .SRT File", visible=False)

    # List of all UI components to be targeted by the restart function
    all_components = [
        info_state, url_input, format_selection, subtitle, video_dropdown,
        audio_dropdown, subtitles_checkbox, convert_button, status_display,
        media_download_button, srt_download_button
    ]

    next_button.click(
        on_next_button_click, inputs=[url_input],
        outputs=[info_state, subtitle, video_dropdown, audio_dropdown, subtitles_checkbox, convert_button, format_selection]
    ).then(lambda: gr.update(visible=True), None, format_selection)

    convert_button.click(
        on_convert_button_click,
        inputs=[url_input, video_dropdown, audio_dropdown, info_state, subtitles_checkbox],
        outputs=[status_display, media_download_button, srt_download_button]
    )
    
    # NEW: Link the restart button to the Python reset function
    restart_button.click(
        fn=restart_app,
        inputs=[],
        outputs=all_components
    )

if __name__ == "__main__":
    gradio_username = os.getenv("GRADIO_USERNAME")
    gradio_password = os.getenv("GRADIO_PASSWORD")
    
    auth_credentials = None
    if gradio_username and gradio_password:
        auth_credentials = (gradio_username, gradio_password)
        logging.info("Authentication enabled.")
    else:
        logging.info("No credentials found. Running in public mode.")
        
    demo.launch(server_name="0.0.0.0", auth=auth_credentials)
