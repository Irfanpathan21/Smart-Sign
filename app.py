import os
import json
import tempfile
from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from moviepy.editor import VideoFileClip, ImageClip, concatenate_videoclips
import PIL.Image
from PIL import ImageDraw, ImageFont
import io

# --- 🟢 CRITICAL FIX FOR RENDER 🟢 ---
# This forces the code to work with NEW Pillow versions (which Render installs)
# by creating a fake 'ANTIALIAS' attribute that MoviePy needs.
if not hasattr(PIL.Image, 'ANTIALIAS'):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
# -------------------------------------

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Use API Key (Simpler method)
DRIVE_API_KEY = os.environ.get("DRIVE_API_KEY")
ROOT_FOLDER_ID = os.environ.get("ROOT_FOLDER_ID")
IGNORED_FOLDERS = ["MHSL - 259", "New 2500 ISL Dictionary Videos", "NCERT 156 new"]

drive_service = None

def setup_drive():
    global drive_service
    if DRIVE_API_KEY:
        try:
            # developerKey is the simple API Key method
            drive_service = build('drive', 'v3', developerKey=DRIVE_API_KEY)
            print("✅ Google Drive Authenticated (API Key)")
        except Exception as e:
            print(f"⚠️ Auth Failed: {e}")
    else:
        print("❌ No API Key found in Environment Variables")

setup_drive()

# --- HELPERS ---

def get_isl_glosses(text):
    model = genai.GenerativeModel('gemini-2.0-flash')
    # Multilingual Prompt
    prompt = f"""
    Act as an Indian Sign Language (ISL) translator.
    Step 1: If input is Hindi/Marathi/Tamil/etc, TRANSLATE to simple English.
    Step 2: Convert to ISL Glosses (Keywords).
    Rules: Keep only content words (Nouns, Verbs). Root verbs. Uppercase. Comma separated.
    Input: "{text}"
    Output:
    """
    try:
        response = model.generate_content(prompt)
        return [w.strip() for w in response.text.replace('\n', '').split(',') if w.strip()]
    except:
        return []

def find_file_in_folder(word, folder_id):
    """Searches for a video inside the specific folder ID provided"""
    if not drive_service or not folder_id: return None
    
    # 1. First find the letter subfolder (e.g., 'A' for Apple)
    first_char = word[0].upper()
    subfolder_name = first_char if first_char.isalpha() else "Numbers"
    
    try:
        # Find subfolder
        q_folder = f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and name='{subfolder_name}' and trashed=false"
        res_folder = drive_service.files().list(q=q_folder, fields="files(id)").execute()
        folders = res_folder.get('files', [])
        
        if not folders: return None
        subfolder_id = folders[0]['id']

        # 2. Search for the word video inside that subfolder
        # We search strictly for the name to avoid grabbing wrong files
        q_file = f"'{subfolder_id}' in parents and mimeType contains 'video' and name contains '{word}' and trashed=false"
        res_file = drive_service.files().list(q=q_file, fields="files(id, name)", pageSize=5).execute()
        files = res_file.get('files', [])

        # Simple Logic: Pick the first one that looks like a match
        # (You can make this smarter later, but this is robust for now)
        for f in files:
            if word in f['name'].upper():
                return {'id': f['id'], 'type': 'video'}
        
        return None
    except Exception as e:
        print(f"Search Error for {word}: {e}")
        return None

def create_placeholder(text):
    try:
        width, height = 1280, 720
        img = PIL.Image.new('RGB', (width, height), (0, 0, 0))
        d = ImageDraw.Draw(img)
        # Default font
        font = ImageFont.load_default()
        # Draw text in center
        d.text((width/2, height/2), text, fill="white", anchor="mm")
        
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        img.save(temp.name)
        return temp.name
    except Exception as e:
        print(f"Image Gen Error: {e}")
        return None

def download_video(file_id):
    try:
        request = drive_service.files().get_media(fileId=file_id)
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        fh = io.FileIO(temp.name, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.close()
        return temp.name
    except:
        return None

# --- ROUTES ---

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/process_sign', methods=['POST'])
def process():
    try:
        if not drive_service or not ROOT_FOLDER_ID:
            return jsonify({"error": "Server Config Error: Check API Keys"}), 500

        text = request.json.get('text', '')
        glosses = get_isl_glosses(text)
        print(f"Glosses: {glosses}")

        clips = []
        
        for word in glosses:
            # Search
            res = find_file_in_folder(word, ROOT_FOLDER_ID)
            
            if res:
                # Video found
                path = download_video(res['id'])
                if path:
                    clip = VideoFileClip(path).resize(newsize=(1280, 720))
                    clips.append(clip)
            else:
                # Image fallback
                path = create_placeholder(word)
                if path:
                    clip = ImageClip(path).set_duration(2).resize(newsize=(1280, 720))
                    clips.append(clip)

        if not clips:
            return jsonify({"error": "No clips found"}), 400

        final = concatenate_videoclips(clips, method="compose")
        output = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
        
        # Write file
        final.write_videofile(output, fps=24, codec='libx264', audio_codec='aac', remove_temp=True)
        
        return send_file(output, mimetype='video/mp4')

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
