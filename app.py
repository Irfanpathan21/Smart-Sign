import os
import json
import tempfile
import glob
from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
from moviepy.editor import VideoFileClip, ImageClip, concatenate_videoclips
from PIL import Image, ImageDraw, ImageFont
import io

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Authentication
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
DRIVE_API_KEY = os.environ.get("DRIVE_API_KEY")

# 🔴 NEW: Get Folder ID directly from Environment to avoid "Search" errors
ROOT_FOLDER_ID = os.environ.get("ROOT_FOLDER_ID") 
ROOT_FOLDER_NAME = "ISL Dictionary" 
IGNORED_FOLDERS = ["MHSL - 259", "New 2500 ISL Dictionary Videos", "NCERT 156 new"]

drive_service = None

def setup_drive():
    global drive_service
    if GOOGLE_CREDENTIALS_JSON:
        try:
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = service_account.Credentials.from_service_account_info(
                creds_dict, scopes=['https://www.googleapis.com/auth/drive.readonly']
            )
            drive_service = build('drive', 'v3', credentials=creds)
            print("✅ Google Drive Authenticated (Service Account)")
            return
        except Exception as e:
            print(f"⚠️ Service Account Auth failed: {e}")

    if DRIVE_API_KEY:
        try:
            drive_service = build('drive', 'v3', developerKey=DRIVE_API_KEY)
            print("✅ Google Drive Authenticated (API Key)")
            return
        except Exception as e:
            print(f"⚠️ API Key Auth failed: {e}")

    print("❌ No valid Google Drive authentication found.")

setup_drive()

# --- HELPER FUNCTIONS ---

def find_folder_id(folder_name, parent_id=None):
    """Attempts to find a folder by name. Fails with API Key if parent_id is None."""
    if not drive_service: return None
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    if parent_id: query += f" and '{parent_id}' in parents"
    
    try:
        results = drive_service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        return files[0]['id'] if files else None
    except Exception as e:
        print(f"Search Error (finding '{folder_name}'): {e}")
        return None

# 🔴 If ID is not in Env, try to search (will likely fail with API Key)
if not ROOT_FOLDER_ID and drive_service:
    print("⚠️ ROOT_FOLDER_ID not found in Env. Attempting to search by name...")
    ROOT_FOLDER_ID = find_folder_id(ROOT_FOLDER_NAME)
else:
    print(f"✅ Using configured Root Folder ID: {ROOT_FOLDER_ID}")


def get_isl_glosses(text):
    model = genai.GenerativeModel('gemini-2.0-flash')
    prompt = f"""
    Convert sentence to ISL Glosses (keywords). Output ONLY keywords separated by commas.
    Remove stopwords. Use Uppercase. Root verbs.
    Input: "{text}"
    """
    response = model.generate_content(prompt)
    return [w.strip() for w in response.text.replace('\n', '').split(',') if w.strip()]

def get_all_files_in_folder(folder_id):
    files_map = {}
    page_token = None
    while True:
        try:
            # 🔴 IMPORTANT: corpora='user' is default, works for public files via API key
            response = drive_service.files().list(
                q=f"'{folder_id}' in parents and mimeType contains 'video' and trashed=false",
                fields="nextPageToken, files(id, name, webViewLink)",
                pageSize=1000, pageToken=page_token
            ).execute()
            for f in response.get('files', []):
                files_map[f['name']] = {'link': f['webViewLink'], 'id': f['id']}
            page_token = response.get('nextPageToken')
            if not page_token: break
        except Exception as e:
            print(f"File List Error (Folder ID: {folder_id}): {e}")
            break
    return files_map

def pick_best_file(word, filenames):
    if not filenames: return None
    model = genai.GenerativeModel('gemini-2.0-flash')
    files_str = "\n".join(filenames[:300]) 
    prompt = f"""
    Find best video match for ISL word: "{word}" from list below.
    Prioritize exact matches. Return "NONE" if no good match.
    Return ONLY the filename.
    ---\n{files_str}\n---
    """
    resp = model.generate_content(prompt).text.strip().replace("'", "").replace('"', "")
    return resp if resp in filenames else None

def create_placeholder_image(text):
    width, height = 1280, 720
    img = Image.new('RGB', (width, height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 100)
    except IOError:
        font = ImageFont.load_default()

    d.text((width/2, height/2), text, fill=(255, 255, 255), anchor="mm", font=font)
    
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file.name)
    return temp_file.name

def search_video_smart(word, root_id):
    if not drive_service or not root_id: return None
    first = word[0].upper()
    sub = first if first.isalpha() else ("Numbers" if first.isdigit() else "A")
    if sub in IGNORED_FOLDERS: return None

    # Searching INSIDE a known folder works fine with API Key
    sid = find_folder_id(sub, parent_id=root_id)
    if not sid: return None

    fmap = get_all_files_in_folder(sid)
    if not fmap: return None

    best = pick_best_file(word, list(fmap.keys()))
    if best:
        data = fmap[best]
        data['type'] = 'video'
        return data
    return None

def download_drive_video(file_id):
    try:
        request = drive_service.files().get_media(fileId=file_id)
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        fh = io.FileIO(temp_file.name, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        fh.close()
        return temp_file.name
    except Exception as e:
        print(f"Download Error: {e}")
        raise e

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process_sign', methods=['POST'])
def process_sign():
    cleanup_files = []
    try:
        if not drive_service:
            return jsonify({"error": "Server Error: Google Drive not connected."}), 500
        if not ROOT_FOLDER_ID:
            return jsonify({"error": "Server Error: 'ROOT_FOLDER_ID' not set in Env Vars."}), 500

        data = request.json
        text = data.get('text', '')
        print(f"Processing: {text}")

        glosses = get_isl_glosses(text)
        sequence = []

        for word in glosses:
            res = search_video_smart(word, ROOT_FOLDER_ID)
            if res:
                sequence.append({'type': 'video', 'id': res['id'], 'word': word})
            else:
                img_path = create_placeholder_image(word)
                sequence.append({'type': 'image', 'path': img_path, 'word': word})
                cleanup_files.append(img_path)

        clips = []
        for item in sequence:
            if item['type'] == 'video':
                vid_path = download_drive_video(item['id'])
                cleanup_files.append(vid_path)
                clip = VideoFileClip(vid_path).resize(newsize=(1280, 720))
                clips.append(clip)
            elif item['type'] == 'image':
                clip = ImageClip(item['path']).set_duration(2).resize(newsize=(1280, 720))
                clips.append(clip)

        if not clips:
            return jsonify({"error": "No content found"}), 400

        final_clip = concatenate_videoclips(clips, method="compose")
        output_path = tempfile.mktemp(suffix=".mp4")
        cleanup_files.append(output_path)
        
        final_clip.write_videofile(output_path, fps=24, codec='libx264', audio_codec='aac', temp_audiofile='temp-audio.m4a', remove_temp=True)

        return send_file(output_path, mimetype='video/mp4')

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    
    finally:
        pass

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
