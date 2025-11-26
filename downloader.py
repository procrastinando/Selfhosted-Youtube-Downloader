import gradio as gr
import yt_dlp
import os
import re
import subprocess
import random
import logging
from collections import defaultdict
from groq import Groq
import groq

# --- Configuration & Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load configuration
GROQ_API_KEYS = [a.strip() for a in os.getenv("GROQ_API_KEYS", "").split(",") if a.strip()]
WHISPER_MODEL = os.getenv("WHISPER_MODEL_ID", "whisper-large-v3")
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

logging.info(f"Loaded {len(GROQ_API_KEYS)} Groq API keys.")

# --- Helper Functions ---
def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

def generate_srt_from_transcription(transcription, srt_path):
    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            if hasattr(transcription, 'segments') and transcription.segments:
                for i, segment in enumerate(transcription.segments, 1):
                    f.write(f"{i}\n")
                    f.write(f"{format_time(segment.get('start', 0))} --> {format_time(segment.get('end', 0))}\n")
                    f.write(f"{segment.get('text', '').strip()}\n\n")
                return True
    except Exception as e:
        logging.error(f"Error writing SRT file: {e}")
    return False

def transcribe_with_groq(api_keys, audio_path, srt_path):
    if not api_keys:
         return {"error": "No Groq API keys found. Please set GROQ_API_KEYS."}
    
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
                return {"success": True, "srt_path": srt_path}
            else:
                return {"error": "Transcription succeeded but no segments were found."}

        except groq.RateLimitError:
            logging.warning(f"Key ...{key[-4:]} hit rate limit. Switching keys.")
            continue
        except Exception as e:
            logging.error(f"Transcription error: {e}")
            return {"error": str(e)}

    return {"error": "All Groq API keys failed."}

# --- YouTube Logic ---
def get_video_info(url):
    ydl_opts = {'quiet': True, 'no_warnings': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            return ydl.extract_info(url, download=False)
        except Exception as e:
            return {"error": str(e)}

def parse_formats(info_dict):
    if "error" in info_dict:
        return [], [], info_dict["error"]
    
    video_formats = defaultdict(list)
    audio_formats_list = []

    for f in info_dict.get('formats', []):
        # Filter for video-only streams
        if f.get('vcodec') != 'none' and f.get('acodec') == 'none':
            res = f.get('height')
            if res:
                note = f"{f.get('format_note', res)}p - {f.get('ext')}"
                video_formats[res].append(note)
        
        # Filter for audio-only streams
        elif f.get('acodec') != 'none' and f.get('vcodec') == 'none':
            abr = f.get('abr')
            if abr:
                audio_formats_list.append((abr, f"{int(abr)}kbps - {f.get('ext')}"))

    # Sort Video: High resolution first
    sorted_resolutions = sorted(video_formats.keys(), reverse=True)
    flat_video_list = ["No video"]
    for res in sorted_resolutions:
        # Deduplicate formats for the same resolution
        flat_video_list.extend(sorted(list(set(video_formats[res])), reverse=True))

    # Sort Audio: High bitrate first
    audio_formats_list.sort(key=lambda x: x[0], reverse=True)
    sorted_audio_formats = list(dict.fromkeys([x[1] for x in audio_formats_list])) # Deduplicate preserving order

    return flat_video_list, sorted_audio_formats, None

def download_media_stream(url, fmt_selector, output_template):
    ydl_opts = {
        'format': fmt_selector,
        'outtmpl': output_template,
        'quiet': True,
        'overwrites': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# --- Gradio Handlers ---

def on_next_click(url):
    if not url:
        raise gr.Error("Please enter a URL")
    
    gr.Info("Fetching video info...")
    info = get_video_info(url)
    v_fmts, a_fmts, error = parse_formats(info)

    if error:
        raise gr.Error(error)
    if not a_fmts:
        raise gr.Error("No valid audio formats found.")

    # Defaults
    def_vid = next((v for v in v_fmts if "1080p" in v), v_fmts[1] if len(v_fmts) > 1 else v_fmts[0])
    def_aud = a_fmts[0] # Best audio

    return {
        info_state: {"title": info.get('title', 'video')},
        format_col: gr.update(visible=True),
        video_dropdown: gr.update(choices=v_fmts, value=def_vid),
        audio_dropdown: gr.update(choices=a_fmts, value=def_aud),
        convert_btn: gr.update(visible=True)
    }

def on_convert_click(url, v_fmt, a_fmt, state, gen_subs):
    title = re.sub(r'[\\/*?:"<>|]', "", state.get("title", "video"))
    base_path = os.path.join(DOWNLOADS_DIR, title)
    
    # UI Reset
    yield {status_md: gr.update(value="⏳ Starting...", visible=True), dl_media_btn: gr.update(visible=False), dl_srt_btn: gr.update(visible=False)}

    video_path = None
    audio_path = None
    srt_path = None

    try:
        # 1. Download Video
        if v_fmt != "No video":
            yield {status_md: "⏳ Downloading Video..."}
            # Extract resolution number (e.g. 1080 from "1080p - mp4")
            res_match = re.search(r'(\d+)', v_fmt)
            res = res_match.group(1) if res_match else "1080"
            download_media_stream(url, f"bestvideo[height<={res}]", f"{base_path}_v.%(ext)s")
            
            # Find file
            for ext in ['mp4', 'webm', 'mkv']:
                p = f"{base_path}_v.{ext}"
                if os.path.exists(p):
                    video_path = p
                    break

        # 2. Download Audio
        yield {status_md: "⏳ Downloading Audio..."}
        # Extract bitrate (e.g. 128 from "128kbps - m4a")
        abr_match = re.search(r'(\d+)', a_fmt)
        abr = abr_match.group(1) if abr_match else "128"
        download_media_stream(url, f"bestaudio[abr<={abr}]/bestaudio", f"{base_path}_a.%(ext)s")
        
        for ext in ['m4a', 'webm', 'opus', 'mp3']:
            p = f"{base_path}_a.{ext}"
            if os.path.exists(p):
                audio_path = p
                break
        
        # 3. Transcribe (Optional)
        transcribe_input = audio_path
        temp_mp3 = None
        
        if gen_subs and audio_path:
            yield {status_md: "⏳ Transcribing (this may take a moment)..."}
            
            # If audio is heavy, compress for API
            if int(abr) > 64:
                temp_mp3 = f"{base_path}_temp.mp3"
                subprocess.run(['ffmpeg', '-y', '-i', audio_path, '-b:a', '64k', temp_mp3], 
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                transcribe_input = temp_mp3

            srt_path = f"{base_path}.srt"
            res = transcribe_with_groq(GROQ_API_KEYS, transcribe_input, srt_path)
            
            if temp_mp3 and os.path.exists(temp_mp3):
                os.remove(temp_mp3)
                
            if "error" in res:
                gr.Warning(f"Subtitle error: {res['error']}")
                srt_path = None

        # 4. Merge/Finalize
        yield {status_md: "⏳ Merging files..."}
        final_file = f"{base_path}.mp4" if video_path else f"{base_path}.mp3"
        
        cmd = ['ffmpeg', '-y']
        
        if video_path and audio_path:
            cmd.extend(['-i', video_path, '-i', audio_path])
            if srt_path:
                cmd.extend(['-i', srt_path, '-c', 'copy', '-c:s', 'mov_text'])
            else:
                cmd.extend(['-c', 'copy'])
            cmd.append(final_file)
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        elif audio_path:
            # Audio only convert to MP3
            cmd.extend(['-i', audio_path, '-b:a', f"{abr}k", final_file])
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Cleanup parts
        if video_path and os.path.exists(video_path): os.remove(video_path)
        if audio_path and os.path.exists(audio_path): os.remove(audio_path)

        yield {
            status_md: "✅ Done!",
            dl_media_btn: gr.update(value=final_file, visible=True),
            dl_srt_btn: gr.update(value=srt_path, visible=bool(srt_path))
        }

    except Exception as e:
        logging.error(e)
        yield {status_md: f"❌ Error: {str(e)}"}

def restart_ui():
    return {
        url_input: gr.update(value=""),
        format_col: gr.update(visible=False),
        video_dropdown: gr.update(value=None),
        audio_dropdown: gr.update(value=None),
        convert_btn: gr.update(visible=False),
        status_md: gr.update(value="", visible=False),
        dl_media_btn: gr.update(value=None, visible=False),
        dl_srt_btn: gr.update(value=None, visible=False),
        info_state: {}
    }

# --- UI Construction ---
with gr.Blocks(theme=gr.themes.Soft(), title="YT Downloader") as demo:
    info_state = gr.State({})

    with gr.Row():
        gr.Markdown("## 📺 YouTube Downloader & Converter")
        restart_btn = gr.Button("🔄 Restart", variant="secondary", scale=0)

    with gr.Row():
        url_input = gr.Textbox(label="YouTube URL", placeholder="https://youtube.com/watch?v=...", scale=4)
        next_btn = gr.Button("Next", variant="primary", scale=1)

    # Format Selection Area
    with gr.Column(visible=False) as format_col:
        gr.Markdown("### Select Quality")
        with gr.Row():
            video_dropdown = gr.Dropdown(label="Video", interactive=True)
            audio_dropdown = gr.Dropdown(label="Audio", interactive=True)
        
        subs_check = gr.Checkbox(label="Generate AI Subtitles (Groq/Whisper)", value=False)
        convert_btn = gr.Button("⬇️ Download & Convert", visible=False, variant="primary")

    # Status & Downloads
    status_md = gr.Markdown(visible=False)
    with gr.Row():
        dl_media_btn = gr.DownloadButton("Download Media", visible=False)
        dl_srt_btn = gr.DownloadButton("Download SRT", visible=False)

    # Events
    next_btn.click(
        on_next_click, 
        inputs=[url_input], 
        outputs=[info_state, format_col, video_dropdown, audio_dropdown, convert_btn]
    )

    convert_btn.click(
        on_convert_click,
        inputs=[url_input, video_dropdown, audio_dropdown, info_state, subs_check],
        outputs=[status_md, dl_media_btn, dl_srt_btn]
    )

    restart_btn.click(
        restart_ui,
        outputs=[url_input, format_col, video_dropdown, audio_dropdown, convert_btn, status_md, dl_media_btn, dl_srt_btn, info_state]
    )

if __name__ == "__main__":
    user = os.getenv("GRADIO_USERNAME")
    pwd = os.getenv("GRADIO_PASSWORD")
    auth = (user, pwd) if user and pwd else None
    
    demo.queue().launch(server_name="0.0.0.0", auth=auth)
