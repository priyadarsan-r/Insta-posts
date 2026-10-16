import os
import time
import asyncio
import requests
from bs4 import BeautifulSoup
from google import genai
from gtts import gTTS
import whisper
import warnings
warnings.filterwarnings("ignore") # Hides PyTorch warnings in the logs
from playwright.async_api import async_playwright
from moviepy.editor import ImageClip, AudioFileClip, TextClip, CompositeVideoClip, concatenate_videoclips

# --- SECRETS & CONFIG ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
IG_USER_ID = os.environ.get("IG_USER_ID")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")

VOICE = "en-US-ChristopherNeural"
OUTPUT_AUDIO = "voiceover.mp3"
OUTPUT_SRT = "subtitles.srt"
OUTPUT_VIDEO = "final_reel.mp4"

async def main():
    # 1. Fetch Trending Repos
    print("[1] Fetching trending GitHub repos...")
    resp = requests.get("https://github.com/trending/python?since=daily")
    soup = BeautifulSoup(resp.text, "html.parser")
    repos = []
    
    for article in soup.find_all('article', class_='Box-row')[:3]:
        repo_name = article.find('h2', class_='h3').text.strip().replace('\n', '').replace(' ', '')
        desc_p = article.find('p', class_='col-9')
        repos.append({
            "name": repo_name, 
            "url": f"https://github.com/{repo_name}", 
            "description": desc_p.text.strip() if desc_p else "Trending repository."
        })

    # 2. Generate Script via Gemini
    print("[2] Generating script...")
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = "Write a fast-paced 45-second voiceover script for an Instagram Reel highlighting these 3 GitHub repos. No visual directions, just the spoken words. Give a quick intro, mention each repo and what it does, and a quick outro:\n" + str(repos)
    
    # NEW: Retry logic to handle 503 High Demand errors
    max_retries = 5
    script_text = ""
    for attempt in range(max_retries):
        try:
            # Note: If 'gemini-3.5-flash-lite' continues to face high demand, 
            # consider falling back to 'gemini-2.5-flash' or 'gemini-1.5-flash'
            response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
            script_text = response.text.replace('*', '').replace('#', '').strip()
            break  # Success! Exit the retry loop
        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                wait_time = 15 * (attempt + 1) # Wait 15s, 30s, 45s, etc.
                print(f"Gemini API busy (503). Retrying in {wait_time}s... (Attempt {attempt + 2}/{max_retries})")
                time.sleep(wait_time)
                if attempt == max_retries - 1:
                    raise Exception(f"Gemini API failed after {max_retries} attempts. Last error: {e}")
            else:
                raise e # Throw immediately if it's a different kind of error (like auth failure)

    # 3. Audio & Subtitles (gTTS + Whisper)
    print("[3] Synthesizing Voice & Subtitles...")
    
    # 3a. Generate Voice using Google TTS
    # tld='com' gives a standard US accent. Use 'co.uk' for British or 'com.au' for Australian
    tts = gTTS(text=script_text, lang='en', tld='com')
    tts.save(OUTPUT_AUDIO)

    # 3b. Generate exact word-level timestamps using local AI
    print("    Transcribing audio to generate exact subtitle timings...")
    model = whisper.load_model("base")
    result = model.transcribe(OUTPUT_AUDIO, word_timestamps=True)

    # 3c. Format the output into standard SRT format for MoviePy
    with open(OUTPUT_SRT, "w", encoding="utf-8") as srt_file:
        srt_index = 1
        for segment in result['segments']:
            for word in segment.get('words', []):
                text = word['word'].strip()
                if not text:
                    continue
                
                # Convert seconds to SRT timestamp format (HH:MM:SS,ms)
                def fmt_time(s):
                    h = int(s // 3600)
                    m = int((s % 3600) // 60)
                    sec = int(s % 60)
                    ms = int((s % 1) * 1000)
                    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"

                srt_file.write(f"{srt_index}\n")
                srt_file.write(f"{fmt_time(word['start'])} --> {fmt_time(word['end'])}\n")
                srt_file.write(f"{text}\n\n")
                srt_index += 1

    # 4. Capture Visuals (Playwright)
    print("[4] Capturing Screenshots...")
    image_paths = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1080, 'height': 1920})
        for i, repo in enumerate(repos):
            # Changed wait_until to "load" and added a 60s timeout
            await page.goto(repo["url"], wait_until="load", timeout=60000)
            
            # Added a 2-second wait to allow dynamic elements/fonts to load
            await page.wait_for_timeout(2000)
            
            path = f"repo_{i}.png"
            await page.screenshot(path=path)
            image_paths.append(path)
        await browser.close()

    # 5. Assemble Video (MoviePy)
    print("[5] Rendering Video...")
    audio = AudioFileClip(OUTPUT_AUDIO)
    time_per_image = audio.duration / len(image_paths)
    bg_clips = [ImageClip(img).set_duration(time_per_image) for img in image_paths]
    base_video = concatenate_videoclips(bg_clips, method="compose").set_audio(audio)
    
    # Parse SRT manually for MoviePy overlay
    with open(OUTPUT_SRT, 'r', encoding='utf-8') as f:
        blocks = f.read().strip().split('\n\n')
    
    subtitle_clips = []
    for block in blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            start_str, end_str = lines[1].split(' --> ')
            def to_sec(t_str):
                h, m, s_ms = t_str.split(':')
                return int(h)*3600 + int(m)*60 + int(s_ms.split(',')[0]) + int(s_ms.split(',')[1])/1000.0
            
            txt_clip = TextClip(
                " ".join(lines[2:]), fontsize=90, color='white', 
                font='Liberation-Sans-Bold', bg_color='rgba(0,0,0,0.6)'
            ).set_start(to_sec(start_str)).set_end(to_sec(end_str)).set_position(('center', 'center'))
            subtitle_clips.append(txt_clip)

    final_video = CompositeVideoClip([base_video] + subtitle_clips)
    final_video.write_videofile(OUTPUT_VIDEO, fps=30, codec="libx264", audio_codec="aac")

    # 6. Publish to Instagram
    print("[6] Publishing to Instagram...")
    api_version = "v19.0"
    if not IG_USER_ID or not IG_ACCESS_TOKEN:
        print("Skipping publish: Missing Instagram secrets.")
        return

    # Container Creation with Error Handling
    res = requests.post(
        f"https://graph.facebook.com/{api_version}/{IG_USER_ID}/media",
        data={"media_type": "REELS", "upload_type": "resumable", "caption": "Trending Repos! 🚀 #github #coding", "access_token": IG_ACCESS_TOKEN}
    ).json()
    
    if "id" not in res:
        print(f"\n❌ INSTAGRAM API ERROR: {res}\n")
        raise Exception("Failed to create Instagram container. See the exact API error printed above.")
        
    container_id = res["id"]
    
    # Binary Upload
    with open(OUTPUT_VIDEO, "rb") as f:
        video_data = f.read()
        
    upload_res = requests.post(
        f"https://rupload.facebook.com/ig-api-upload/{api_version}/{container_id}",
        headers={
            "Authorization": f"OAuth {IG_ACCESS_TOKEN}", 
            "offset": "0",
            "file_offset": "0",
            "X-Entity-Name": OUTPUT_VIDEO,
            "X-Entity-Length": str(len(video_data)),
            "Content-Length": str(len(video_data)),
            "Content-Type": "application/octet-stream"
        },
        data=video_data
    )
    if upload_res.status_code != 200:
        raise Exception(f"Failed to upload binary: {upload_res.text}")
        
    # Polling Processing Status
    for _ in range(20):
        status_res = requests.get(
            f"https://graph.facebook.com/{api_version}/{container_id}", 
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}
        ).json()
        status = status_res.get("status_code")
        
        if status == "FINISHED": 
            break
        elif status == "ERROR":
            raise Exception("Instagram failed to process the uploaded video format.")
        time.sleep(15)
        
    # Final Publish Execution
    pub_res = requests.post(
        f"https://graph.facebook.com/{api_version}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    ).json()
    
    if "id" in pub_res:
        print(f"✅ Published! Reel ID: {pub_res.get('id')}")
    else:
        print(f"\n❌ INSTAGRAM PUBLISH ERROR: {pub_res}\n")
if __name__ == "__main__":
    asyncio.run(main())
