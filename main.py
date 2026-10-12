import os
import time
import asyncio
import requests
from bs4 import BeautifulSoup
from google import genai
import edge_tts
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
    response = client.models.generate_content(model='gemini-3.5-flash-lite', contents=prompt)
    script_text = response.text.replace('*', '').replace('#', '').strip()

    # 3. Audio & Subtitles (Edge-TTS)
    print("[3] Synthesizing Voice & Subtitles...")
    communicate = edge_tts.Communicate(script_text, VOICE)
    submaker = edge_tts.SubMaker()
    
    with open(OUTPUT_AUDIO, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                submaker.feed(chunk)
                
    with open(OUTPUT_SRT, "w", encoding="utf-8") as srt_file:
        srt_file.write(submaker.get_srt())

    # 4. Capture Visuals (Playwright)
    print("[4] Capturing Screenshots...")
    image_paths = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1080, 'height': 1920})
        for i, repo in enumerate(repos):
            await page.goto(repo["url"], wait_until="networkidle")
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

    # Container Creation
    res = requests.post(
        f"https://graph.facebook.com/{api_version}/{IG_USER_ID}/media",
        data={"media_type": "REELS", "upload_type": "resumable", "caption": "Trending Repos! 🚀 #github #coding", "access_token": IG_ACCESS_TOKEN}
    ).json()
    container_id = res["id"]
    
    # Binary Upload
    with open(OUTPUT_VIDEO, "rb") as f:
        requests.post(
            f"https://rupload.facebook.com/ig-api-upload/{api_version}/{container_id}",
            headers={"Authorization": f"OAuth {IG_ACCESS_TOKEN}", "offset": "0"},
            data=f.read()
        )
        
    # Polling Processing Status
    for _ in range(20):
        status = requests.get(
            f"https://graph.facebook.com/{api_version}/{container_id}", 
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}
        ).json().get("status_code")
        if status == "FINISHED": break
        time.sleep(15)
        
    # Final Publish Execution
    pub_res = requests.post(
        f"https://graph.facebook.com/{api_version}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    ).json()
    print(f"Published! Reel ID: {pub_res.get('id')}")

if __name__ == "__main__":
    asyncio.run(main())
