import gradio as gr
import yt_dlp
import os
import re
import subprocess
from collections import defaultdict
from groq import Groq
import groq
import random

# --- Configuration & Setup ---
GROQ_API_KEYS = [a.strip() for a in os.getenv("GROQ_API_KEYS", "").split(",") if a.strip()]
WHISPER_MODEL=os.getenv("WHISPER_MODEL_ID")
USER_ID=os.getenv("USER_ID")
USER_PASSWORD=os.getenv("USER_PASSWORD")
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

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
    """
    Tries to transcribe audio using a list of Groq API keys, shuffling them to distribute load.
    Retries on rate limit errors.
    """
    if not api_keys or not api_keys[0].startswith("gsk_"):
         return {"error": "No valid Groq API keys found in the script. Please add them to the GROQ_API_KEYS list."}
    
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
                # Success!
                return {"success": True, "srt_path": srt_path}
            else:
                return {"error": "Transcription succeeded but no segments were found."}

        except groq.RateLimitError:
            print(f"Key starting with '{key[:7]}...' hit a rate limit. Trying next key.")
            continue  # Try the next key in the list
        except groq.PermissionDeniedError:
            print(f"Key starting with '{key[:7]}...' is invalid or blocked. Trying next key.")
            continue # Try the next key
        except Exception as e:
            return {"error": f"An unexpected error occurred during transcription: {e}"}

    # If the loop finishes, all keys have failed
    return {"error": "All Groq API keys failed due to rate limits or errors."}

# --- YouTube Download Functions ---
def get_video_info(url):
    ydl_opts = {'quiet': True}
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
    """A generic function to download media using yt-dlp."""
    ydl_opts = {
        'format': format_selector,
        'outtmpl': output_path_template,
        'merge_output_format': 'mp4',
        'overwrites': True,
        'quiet': True,
    }
    if postprocessor_hooks:
        ydl_opts['postprocessors'] = postprocessor_hooks

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return True

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
    yield {
        media_download_button: gr.update(visible=False),
        srt_download_button: gr.update(visible=False),
    }
    
    sanitized_title = re.sub(r'[\\/*?:"<>|]', "", info_state["title"])
    base_filepath = os.path.join(DOWNLOADS_DIR, sanitized_title)
    
    # Step 1: Download main media
    gr.Info("Downloading selected media...")
    main_output_file = None
    try:
        if video_format_str == "No video":
            audio_bitrate = re.search(r'(\d+)kbps', audio_format_str).group(1)
            selector = f'bestaudio[abr<={audio_bitrate}]/bestaudio'
            pp_hooks = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]
            download_media(url, selector, f"{base_filepath}.%(ext)s", pp_hooks)
            main_output_file = f"{base_filepath}.mp3"
        else:
            video_res = re.search(r'(\d+)p', video_format_str).group(1)
            audio_bitrate = re.search(r'(\d+)kbps', audio_format_str).group(1)
            selector = f'bestvideo[height<={video_res}]+bestaudio[abr<={audio_bitrate}]/best[height<={video_res}]'
            download_media(url, selector, f"{base_filepath}.%(ext)s")
            main_output_file = f"{base_filepath}.mp4"
    except Exception as e:
        gr.Error(f"Failed to download media: {e}")
        return

    srt_file_path = None
    if generate_subs:
        # Step 2: Download audio for transcription
        gr.Info("Getting audio for transcription...")
        transcription_audio_path = f"{base_filepath}_transcribe.mp3"
        try:
            download_media(
                url, 'bestaudio[abr<=70]/bestaudio', 
                f"{base_filepath}_transcribe.%(ext)s", 
                [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]
            )
        except Exception as e:
            gr.Error(f"Failed to download transcription audio: {e}")
            # Yield the main file even if transcription fails
            yield { media_download_button: gr.update(value=main_output_file, visible=True) }
            return
            
        # Step 3: Transcribe with Groq
        gr.Info("Generating subtitles with Groq API... This may take a while.")
        srt_file_path = f"{base_filepath}.srt"
        transcription_result = transcribe_with_groq(GROQ_API_KEYS, transcription_audio_path, srt_file_path)
        if os.path.exists(transcription_audio_path):
            os.remove(transcription_audio_path)

        if "error" in transcription_result:
            gr.Warning(transcription_result["error"])
            srt_file_path = None # Ensure no download button appears on failure
        else:
            gr.Info("Subtitle generation successful!")

        # Step 4: Merge subtitles if it's a video file
        if srt_file_path and main_output_file.endswith(".mp4"):
            gr.Info("Merging subtitles into video file...")
            video_with_subs_path = f"{base_filepath}_with_subs.mp4"
            try:
                subprocess.run([
                    'ffmpeg', '-y', '-i', main_output_file, '-i', srt_file_path,
                    '-c', 'copy', '-c:s', 'mov_text', video_with_subs_path
                ], check=True, capture_output=True)
                os.remove(main_output_file)
                os.rename(video_with_subs_path, main_output_file)
            except Exception as e:
                gr.Error(f"Failed to merge subtitles: {e}")

    gr.Info("Process Complete!")
    yield {
        media_download_button: gr.update(value=main_output_file, visible=True),
        srt_download_button: gr.update(value=srt_file_path, visible=bool(srt_file_path))
    }

# --- Gradio Interface Definition ---
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    gr.Markdown("# YouTube Video Downloader & Converter")
    info_state = gr.State({})
    
    with gr.Column():
        url_input = gr.Textbox(label="YouTube Video Link", placeholder="Enter YouTube URL here...")
        next_button = gr.Button("Next")
        
    with gr.Column(visible=False) as format_selection:
        subtitle = gr.Markdown("## Choose the format", visible=False)
        video_dropdown = gr.Dropdown(label="Video Quality", visible=False)
        audio_dropdown = gr.Dropdown(label="Audio Quality", visible=False)
        subtitles_checkbox = gr.Checkbox(label="Generate subtitles", value=False, visible=False)
        convert_button = gr.Button("Convert", visible=False)
        
    with gr.Row():
        media_download_button = gr.DownloadButton(label="Download Media File", visible=False)
        srt_download_button = gr.DownloadButton(label="Download .SRT File", visible=False)

    next_button.click(
        on_next_button_click, inputs=[url_input],
        outputs=[info_state, subtitle, video_dropdown, audio_dropdown, subtitles_checkbox, convert_button, format_selection]
    ).then(lambda: gr.update(visible=True), None, format_selection)

    convert_button.click(
        on_convert_button_click,
        inputs=[url_input, video_dropdown, audio_dropdown, info_state, subtitles_checkbox],
        outputs=[media_download_button, srt_download_button]
    )

if __name__ == "__main__":
    demo.launch(title="Youtube Video Downloader", server_name="0.0.0.0", auth=(USER_ID, USER_PASSWORD))