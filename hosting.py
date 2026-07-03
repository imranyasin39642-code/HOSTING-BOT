import os
import sys
import time
import math
import socket
import shutil
import sqlite3
import zipfile
import hashlib
import json
import logging
import platform
import subprocess
import asyncio
from datetime import datetime
from dotenv import load_dotenv
import psutil
import httpx
try:
    from aiohttp import web as aiohttp_web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Force working directory to script directory
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir:
    os.chdir(script_dir)

# Load environment variables
load_dotenv()

# Logger configuration
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress external library loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Single instance lock check using socket
try:
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock_socket.bind(('127.0.0.1', 54321))
except socket.error:
    logger.error("\n" + "="*70 + "\n"
                 "❌ ERROR: Another instance of this hosting bot is already running!\n"
                 "Please close all other terminals/processes of hosting.py first.\n" + "="*70)
    sys.exit(1)

# Constants
raw_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
if "TELEGRAM_BOT_TOKEN" in raw_bot_token:
    raw_bot_token = raw_bot_token.split("=")[-1]
BOT_TOKEN = raw_bot_token.strip().strip('"').strip("'")

raw_owner_id = os.getenv("OWNER_ID", "7062700056")
if "OWNER_ID" in raw_owner_id:
    raw_owner_id = raw_owner_id.split("=")[-1]
owner_id_digits = "".join(c for c in raw_owner_id if c.isdigit())
OWNER_ID = int(owner_id_digits) if owner_id_digits else 7062700056


# Ensure directories exist
os.makedirs("database", exist_ok=True)
os.makedirs("bots", exist_ok=True)
os.makedirs("logs", exist_ok=True)

DB_PATH = os.path.join("database", "bots.db")

# Initialize database
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        token TEXT NOT NULL,
        folder TEXT NOT NULL,
        startup_file TEXT NOT NULL,
        pid INTEGER DEFAULT NULL,
        status TEXT DEFAULT 'Stopped',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_started TIMESTAMP DEFAULT NULL,
        restart_count INTEGER DEFAULT 0,
        uploaded_by INTEGER DEFAULT NULL,
        status_broadcast INTEGER DEFAULT 0
    )
    """)
    # Migration: check if uploaded_by column exists, if not add it
    try:
        cursor.execute("ALTER TABLE bots ADD COLUMN uploaded_by INTEGER DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
        
    # Migration: check if status_broadcast column exists, if not add it
    try:
        cursor.execute("ALTER TABLE bots ADD COLUMN status_broadcast INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS allowed_users (
        user_id INTEGER PRIMARY KEY,
        allowed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # Migration: check if max_bots column exists, if not add it
    try:
        cursor.execute("ALTER TABLE allowed_users ADD COLUMN max_bots INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

init_db()

def get_setting(key, default=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default

def save_setting(key, value):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save setting {key}: {e}")

# User State Management
USER_STATES = {}
BOT_CPU_SPIKES = {}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn



# Helper functions
def is_process_running(pid):
    if not pid:
        return False
    try:
        if platform.system().lower() == "windows":
            try:
                p = psutil.Process(pid)
                return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            except Exception:
                return False
        else:
            try:
                os.kill(pid, 0)
                try:
                    p = psutil.Process(pid)
                    if p.status() == psutil.STATUS_ZOMBIE:
                        return False
                except Exception:
                    pass
                return True
            except (ProcessLookupError, OSError):
                return False
            except PermissionError:
                return True
    except Exception:
        return False

async def get_folder_size_async(path):
    return await asyncio.to_thread(get_folder_size, path)

def get_folder_size(path):
    total_size = 0
    if os.path.exists(path):
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
    return total_size

def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def get_running_time(last_started_str):
    if not last_started_str:
        return "N/A"
    try:
        last_started = datetime.strptime(last_started_str, "%Y-%m-%d %H:%M:%S")
        delta = datetime.now() - last_started
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        return f"{hours}h {minutes}m {seconds}s"
    except Exception:
        return "Error parsing time"

async def start_bot_process(bot_id, run_install=True, update=None, reset_restart_count=True):
    import re
    conn = get_db_connection()
    bot_row = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot_row:
        return False, "Bot not found in database."
        
    bot = dict(bot_row)
    folder = bot["folder"]
    startup_file = bot["startup_file"]
    startup_path = os.path.join(folder, startup_file)
    
    if not os.path.exists(folder):
        return False, f"Folder {folder} does not exist."
    if not os.path.exists(startup_path):
        return False, f"Startup file {startup_file} not found in {folder}."
        
    # Read/Verify .env file if it exists
    env_path = os.path.join(folder, ".env")
    token_pattern = re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{35})\b")
    token_in_env = None
    
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()
            match = token_pattern.search(content)
            if match:
                token_in_env = match.group(1)
        except Exception:
            pass
            
    bot_token = bot["token"]
    
    # Sync token if .env has a different valid one
    if token_in_env and token_in_env != bot_token:
        success, bot_name, bot_username = await verify_token_and_get_details(token_in_env)
        if success:
            conn = get_db_connection()
            conn.execute("UPDATE bots SET token = ?, name = ? WHERE id = ?", (token_in_env, bot_name, bot_id))
            conn.commit()
            conn.close()
            bot_token = token_in_env
            logger.info(f"Synchronized bot token in database from .env for bot {bot_id}: {bot_name}")
        else:
            return False, (
                f"❌ <b>Invalid Bot Token in .env!</b>\n\n"
                f"The token in your .env file is invalid.\n"
                f"API Error: <code>{bot_name}</code>\n\n"
                f"💡 Please upload a correct .env file or update the token."
            )
            
    # If no token in .env and no valid token in database:
    if not token_pattern.search(bot_token):
        return False, (
            "❌ <b>Bot Token Missing!</b>\n\n"
            "We could not find a valid Telegram Bot Token in your .env file or database.\n\n"
            "💡 <b>How to fix this:</b>\n"
            "1. Use the 📁 <b>File Manager</b> to upload a `.env` file containing:\n"
            "   <code>BOT_TOKEN=your_token_from_botfather</code>\n"
            "2. Or edit the existing `.env` file to insert your token."
        )
        
    # Verify the current token via API before manual starting (run_install=True)
    if run_install:
        success, bot_name, bot_username = await verify_token_and_get_details(bot_token)
        if not success:
            return False, (
                f"❌ <b>Bot Token Verification Failed!</b>\n\n"
                f"Telegram API rejected the token: <code>{bot_token[:9]}...</code>\n"
                f"Error: <code>{bot_name}</code>\n\n"
                f"💡 Please upload a valid .env file containing the correct token."
            )

    # Make sure .env contains the token
    if not os.path.exists(env_path):
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(f"BOT_TOKEN={bot_token}\nTELEGRAM_BOT_TOKEN={bot_token}\n")
        except Exception as e:
            logger.error(f"Failed to create .env on startup: {e}")
    else:
        # Ensure it has BOT_TOKEN
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "BOT_TOKEN" not in content and "TELEGRAM_BOT_TOKEN" not in content:
                with open(env_path, "a", encoding="utf-8") as f:
                    f.write(f"\nBOT_TOKEN={bot_token}\nTELEGRAM_BOT_TOKEN={bot_token}\n")
        except Exception:
            pass

    # Automatic Dependency Verification & Installation on startup
    if run_install:
        try:
            await install_bot_dependencies(folder, bot["name"])
        except Exception as de:
            logger.error(f"Dependency installation failed on start: {de}")
            return False, f"❌ Dependency installation failed: {de}"
            
    log_file_path = os.path.join("logs", f"bot_{bot_id}.log")

    env = os.environ.copy()
    # ── Load ALL variables from the bot's .env file ──────────────────
    # This is critical: API_ID, API_HASH, SESSION_STRING, DB_URL, etc.
    # must all reach the child process — not just the Telegram token.
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as _ef:
                for _line in _ef:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _key, _, _val = _line.partition("=")
                    _key = _key.strip()
                    _val = _val.strip().strip('"').strip("'")
                    if _key:
                        env[_key] = _val
        except Exception as _ee:
            logger.warning(f"Could not load .env for bot {bot_id}: {_ee}")

    # Always ensure the Telegram token is set (covers both naming conventions)
    env["BOT_TOKEN"] = bot_token
    env["TELEGRAM_BOT_TOKEN"] = bot_token
    # Disable stdout buffering so logs appear instantly in the log file
    env["PYTHONUNBUFFERED"] = "1"
    # Inject PYTHONPATH so the bot can import packages installed in its local lib/ dir
    bot_abs_folder = os.path.abspath(folder)
    local_lib = os.path.join(bot_abs_folder, "lib")
    env["PYTHONPATH"] = local_lib + os.pathsep + bot_abs_folder + os.pathsep + env.get("PYTHONPATH", "")
    
    async def spawn_process():
        log_f = open(log_file_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, startup_file],
            cwd=folder,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            text=True
        )
        log_f.close()
        return proc

    try:
        process = await spawn_process()
        
        # Set below-normal CPU priority to prevent host CPU starvation
        try:
            p = psutil.Process(process.pid)
            if sys.platform == "win32":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            else:
                p.nice(10)
            logger.info(f"Set below-normal CPU priority nice level for bot {bot_id} (PID: {process.pid})")
        except Exception as pe:
            logger.warning(f"Could not set process priority for bot {bot_id}: {pe}")
            
        # Sleep for a short moment to check if process crashes immediately
        await asyncio.sleep(2.0)
        if process.poll() is not None:
            # Process exited immediately! Read the last 15 lines of log file
            error_details = "Process exited immediately."
            if os.path.exists(log_file_path):
                try:
                    with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                        lines = lf.readlines()
                        error_details = "".join(lines[-15:])
                except Exception:
                    pass
            
            conn = get_db_connection()
            conn.execute("UPDATE bots SET pid = NULL, status = 'Stopped' WHERE id = ?", (bot_id,))
            conn.commit()
            conn.close()
            return False, f"❌ <b>Bot crashed on startup!</b>\n\nError traceback:\n<pre>{error_details}</pre>"
            
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db_connection()
        if reset_restart_count:
            conn.execute(
                "UPDATE bots SET pid = ?, status = 'Running', last_started = ?, restart_count = 0 WHERE id = ?",
                (process.pid, now_str, bot_id)
            )
        else:
            conn.execute(
                "UPDATE bots SET pid = ?, status = 'Running', last_started = ? WHERE id = ?",
                (process.pid, now_str, bot_id)
            )
        conn.commit()
        conn.close()
        try:
            asyncio.create_task(broadcast_bot_status(bot_id, "online"))
        except Exception:
            pass
        return True, process.pid
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        return False, str(e)

def is_sqlite_db(file_path):
    if not os.path.isfile(file_path):
        return False
    if file_path.endswith(('.db', '.sqlite', '.sqlite3', '.sql', '.session')):
        return True
    try:
        if os.path.getsize(file_path) < 100:
            return False
        with open(file_path, 'rb') as f:
            header = f.read(15)
        return header == b'SQLite format 3'
    except Exception:
        return False

async def calculate_db_hash_async(folder):
    return await asyncio.to_thread(calculate_db_hash, folder)

def calculate_db_hash(folder):
    db_files = []
    for root, dirs, filenames in os.walk(folder):
        if "backups" in root:
            continue
        for file in filenames:
            fp = os.path.join(root, file)
            # Track SQLite files (including standard database files and Pyrogram sessions) and .json files
            if is_sqlite_db(fp) or file.endswith('.json'):
                db_files.append(fp)
                # Check for associated WAL, SHM, and journal files
                for suffix in ["-wal", "-shm", "-journal"]:
                    assoc = fp + suffix
                    if os.path.exists(assoc) and os.path.isfile(assoc):
                        db_files.append(assoc)
    if not db_files:
        return None
    hasher = hashlib.md5()
    for db_file in sorted(list(set(db_files))):
        try:
            st = os.stat(db_file)
            hasher.update(db_file.encode('utf-8'))
            hasher.update(str(st.st_mtime).encode('utf-8'))
            hasher.update(str(st.st_size).encode('utf-8'))
        except Exception:
            pass
    return hasher.hexdigest()

async def backup_bot_databases_async(bot_id, force=False):
    return await asyncio.to_thread(backup_bot_databases, bot_id, force)

def backup_bot_databases(bot_id, force=False):
    conn = get_db_connection()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    if not bot:
        return
        
    folder = bot["folder"]
    backup_dir = os.path.join(folder, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    current_hash = calculate_db_hash(folder)
    if not current_hash:
        return
        
    hash_file = os.path.join(backup_dir, ".last_db_hash")
    if not force and os.path.exists(hash_file):
        try:
            with open(hash_file, "r", encoding="utf-8") as f:
                last_hash = f.read().strip()
            if current_hash == last_hash:
                logger.info(f"Database files for bot {bot_id} did not change. Backup skipped to save space.")
                return
        except Exception as e:
            logger.error(f"Error reading backup hash for bot {bot_id}: {e}")
            
    db_files = []
    for root, dirs, filenames in os.walk(folder):
        if "backups" in root:
            continue
        for file in filenames:
            fp = os.path.join(root, file)
            if is_sqlite_db(fp) or file.endswith('.json'):
                db_files.append(fp)
                for suffix in ["-wal", "-shm", "-journal"]:
                    assoc = fp + suffix
                    if os.path.exists(assoc) and os.path.isfile(assoc):
                        db_files.append(assoc)
                        
    db_files = sorted(list(set(db_files)))
    if not db_files:
        return
        
    zip_name = f"db_backup_{int(time.time())}.zip"
    zip_path = os.path.join(backup_dir, zip_name)
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            for db_file in db_files:
                arcname = os.path.relpath(db_file, folder)
                zipf.write(db_file, arcname)
        logger.info(f"Database backup ZIP created for bot {bot_id} at {zip_path}")
        
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(current_hash)
            
        backups = sorted([f for f in os.listdir(backup_dir) if f.startswith("db_backup_")])
        if len(backups) > 5:
            for old_backup in backups[:-5]:
                try:
                    os.remove(os.path.join(backup_dir, old_backup))
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to backup databases for bot {bot_id}: {e}")

def create_system_backup():
    backup_zip_path = "system_backup.zip"
    
    # Delete existing backup file first
    if os.path.exists(backup_zip_path):
        try:
            os.remove(backup_zip_path)
        except Exception:
            pass
            
    try:
        with zipfile.ZipFile(backup_zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
            # 1. Add key root files
            for fn in ["hosting.py", "requirements.txt", "welcome.png", ".env"]:
                if os.path.exists(fn):
                    zipf.write(fn, fn)
                    
            # 2. Add database directory
            if os.path.exists("database"):
                for root, dirs, files in os.walk("database"):
                    parts = root.split(os.sep)
                    if any(p in ("venv", ".venv", "__pycache__", "logs") for p in parts):
                        continue
                    for file in files:
                        fp = os.path.join(root, file)
                        if file.endswith(("-journal", "-wal", "-shm")):
                            continue
                        zipf.write(fp, os.path.relpath(fp, "."))
                        
            # 3. Add bots directory
            if os.path.exists("bots"):
                for root, dirs, files in os.walk("bots"):
                    parts = root.split(os.sep)
                    if any(p in ("venv", ".venv", "__pycache__", "logs") for p in parts):
                        continue
                    if any(p.startswith("temp_") for p in parts):
                        continue
                    for file in files:
                        fp = os.path.join(root, file)
                        if file.endswith((".zip", ".session", ".session-journal", "-journal", "-wal", "-shm")):
                            continue
                        zipf.write(fp, os.path.relpath(fp, "."))
                        
        logger.info(f"System backup ZIP successfully created at {backup_zip_path}")
        return True, backup_zip_path
    except Exception as e:
        logger.error(f"Failed to create system backup: {e}")
        return False, str(e)

async def extract_telegram_ids_async(folder):
    return await asyncio.to_thread(extract_telegram_ids, folder)

def extract_telegram_ids(folder):
    user_ids = set()
    for root, dirs, filenames in os.walk(folder):
        if "backups" in root:
            continue
        for file in filenames:
            fp = os.path.join(root, file)
            if is_sqlite_db(fp):
                try:
                    conn = sqlite3.connect(fp)
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [row[0] for row in cursor.fetchall()]
                    
                    user_keywords = ["user", "chat", "member", "client", "customer", "subscriber", "tg", "data", "bot"]
                    id_keywords = ["id", "user_id", "chat_id", "telegram_id", "uid", "cid"]
                    
                    for table in tables:
                        if any(kw in table.lower() for kw in user_keywords):
                            try:
                                cursor.execute(f"PRAGMA table_info({table})")
                                columns = [col[1] for col in cursor.fetchall()]
                                for col in columns:
                                    if any(ikw == col.lower() or col.lower().endswith(f"_{ikw}") for ikw in id_keywords):
                                        try:
                                            cursor.execute(f"SELECT DISTINCT {col} FROM {table}")
                                            rows = cursor.fetchall()
                                            for r in rows:
                                                val = r[0]
                                                if isinstance(val, int) and val > 10000:
                                                    user_ids.add(val)
                                                elif isinstance(val, str) and val.isdigit():
                                                    user_ids.add(int(val))
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                    conn.close()
                except Exception as e:
                    logger.error(f"Error scanning sqlite db {fp} for user IDs: {e}")
            elif file.endswith(".json"):
                try:
                    import json
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        data = json.load(f)
                    
                    def scan_dict(d):
                        if isinstance(d, dict):
                            for k, v in d.items():
                                if any(ikw in k.lower() for ikw in ["user_id", "chat_id", "telegram_id", "uid"]):
                                    if isinstance(v, int) and v > 10000:
                                        user_ids.add(v)
                                    elif isinstance(v, str) and v.isdigit():
                                        user_ids.add(int(v))
                                scan_dict(v)
                        elif isinstance(d, list):
                            for item in d:
                                scan_dict(item)
                                
                    scan_dict(data)
                except Exception:
                    pass
    return list(user_ids)

async def broadcast_bot_status(bot_id, status):
    conn = get_db_connection()
    bot_info = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    if not bot_info:
        return
        
    # Check if broadcast is enabled for this bot
    if not bot_info["status_broadcast"]:
        logger.info(f"Broadcast disabled for bot {bot_info['name']} (ID: {bot_id})")
        return
        
    bot_token = bot_info["token"]
    bot_name = bot_info["name"]
    folder = bot_info["folder"]
    
    # Extract user IDs
    user_ids = extract_telegram_ids(folder)
    if not user_ids:
        logger.info(f"No user/chat IDs found in database for bot {bot_name} (ID: {bot_id}) to broadcast.")
        return
        
    logger.info(f"Found {len(user_ids)} users for bot {bot_name} (ID: {bot_id}). Starting status broadcast ({status})...")
    
    from telegram import Bot
    from telegram.error import TelegramError
    
    try:
        target_bot = Bot(token=bot_token)
    except Exception as e:
        logger.error(f"Failed to initialize bot client for broadcast: {e}")
        return
        
    if status == "online":
        text = f"🟢 <b>{bot_name} is now ONLINE!</b>\n\nAll systems are fully functional and ready to use."
    else:
        text = f"🔴 <b>{bot_name} is now OFFLINE!</b>\n\nWe are currently undergoing maintenance. Please check back later."
        
    for uid in user_ids:
        try:
            await target_bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            await asyncio.sleep(0.05) # Rate limit safety
        except TelegramError as te:
            logger.warning(f"Failed to send status update to {uid} via {bot_name}: {te}")
        except Exception as ex:
            logger.error(f"Error in broadcast to {uid}: {ex}")

def stop_bot_process(bot_id):
    conn = get_db_connection()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        return False, "Bot not found."
        
    pid = bot["pid"]
    if pid:
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
        except Exception:
            pass
            
    conn = get_db_connection()
    conn.execute("UPDATE bots SET pid = NULL, status = 'Stopped' WHERE id = ?", (bot_id,))
    conn.commit()
    conn.close()
    
    # Automatically backup databases AFTER the process has stopped completely
    backup_bot_databases(bot_id, force=True)
    try:
        asyncio.create_task(broadcast_bot_status(bot_id, "offline"))
    except Exception:
        pass
    return True, "Bot stopped successfully."

async def perform_network_speedtest():
    """Measure real download and upload speed by counting actual bytes received/sent."""
    try:
        # ── Download: 10 MB test file from Cloudflare ──────────────────
        dl_url = "https://speed.cloudflare.com/__down?bytes=10000000"
        async with httpx.AsyncClient(timeout=30.0) as client:
            t0 = time.time()
            bytes_downloaded = 0
            async with client.stream("GET", dl_url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    bytes_downloaded += len(chunk)
            dl_elapsed = time.time() - t0
            dl_speed_mbps = (bytes_downloaded * 8) / (dl_elapsed * 1_000_000) if dl_elapsed > 0 else 0.0

        # ── Upload: 3 MB payload to Cloudflare ─────────────────────────
        ul_payload = b"\x00" * (3 * 1024 * 1024)  # 3 MB
        async with httpx.AsyncClient(timeout=30.0) as client:
            t0 = time.time()
            resp = await client.post(
                "https://speed.cloudflare.com/__up",
                content=ul_payload,
                headers={"Content-Type": "application/octet-stream"}
            )
            ul_elapsed = time.time() - t0
            if resp.status_code in (200, 201, 400):  # HF returns 400 but upload still succeeded
                ul_speed_mbps = (len(ul_payload) * 8) / (ul_elapsed * 1_000_000) if ul_elapsed > 0 else 0.0
            else:
                ul_speed_mbps = 0.0

        return True, dl_speed_mbps, ul_speed_mbps
    except Exception as e:
        logger.error(f"Network speed test failed: {e}")
        return False, 0.0, 0.0

async def send_speed_stats(message, user_id, is_callback=False):
    start_time = time.time()
    if not is_callback:
        msg = await message.reply_text(
            "⚡ <b>Analyzing system performance...</b>",
            parse_mode="HTML"
        )
    else:
        msg = message
        try:
            await msg.edit_text("⚡ <b>Analyzing system performance...</b>", parse_mode="HTML")
        except Exception:
            pass
            
    latency = (time.time() - start_time) * 1000
    
    # Use interval=1 for a more accurate CPU reading
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('.')
    
    conn = get_db_connection()
    total_bots = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    conn.close()
    
    running_bots = 0
    conn = get_db_connection()
    bots = conn.execute("SELECT pid FROM bots").fetchall()
    conn.close()
    for b in bots:
        if is_process_running(b["pid"]):
            running_bots += 1

    # Calculate total RAM used by all hosted child bots
    bots_ram_used = 0
    conn = get_db_connection()
    running_bot_pids = conn.execute("SELECT pid FROM bots WHERE pid IS NOT NULL AND status = 'Running'").fetchall()
    conn.close()
    for b in running_bot_pids:
        try:
            p = psutil.Process(b["pid"])
            bots_ram_used += p.memory_info().rss
            for child in p.children(recursive=True):
                try:
                    bots_ram_used += child.memory_info().rss
                except Exception:
                    pass
        except Exception:
            pass
            
    def make_progress_bar(percent):
        filled = int(round(percent / 10))
        bar = "█" * filled + "░" * (10 - filled)
        return f"<code>[{bar}] {percent:.1f}%</code>"
        
    performance_text = (
        "⚡ <b>GAMEOVER VPS — SYSTEM MONITOR</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>API Latency</b>: <code>{latency:.2f}ms</code>\n"
        f"💻 <b>CPU Load</b>: {make_progress_bar(cpu_percent)}\n"
        f"🧠 <b>RAM Usage</b>: {make_progress_bar(memory.percent)} (<code>{format_size(memory.used)} / {format_size(memory.total)}</code>)\n"
        f"🤖 <b>Bots RAM</b>: <code>{format_size(bots_ram_used)}</code> used by hosted bots\n"
        f"💽 <b>Disk Space</b>: {make_progress_bar(disk.percent)} (<code>{format_size(disk.used)} / {format_size(disk.total)}</code>)\n"
        f"🤖 <b>Active Bots</b>: <code>{running_bots} / {total_bots} Running</code>\n"
        f"🕒 <b>Last Updated</b>: <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <i>Real-time hardware status. Press ⚡ Run Speedtest for network speeds.</i>"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh Hardware", callback_data="speed_refresh"),
            InlineKeyboardButton("⚡ Run Net Speedtest", callback_data="speedtest_run")
        ],
        [
            InlineKeyboardButton("❌ Close Stats", callback_data="close_menu")
        ]
    ])
    
    try:
        await msg.edit_text(performance_text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to edit speed stats: {e}")

# Keyboards
ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Upload Bot"), KeyboardButton("📂 My Bots")],
        [KeyboardButton("▶ Start Bot"), KeyboardButton("⏹ Stop Bot"), KeyboardButton("🔄 Restart Bot")],
        [KeyboardButton("📊 Statistics"), KeyboardButton("📜 Logs")],
        [KeyboardButton("👤 Profile"), KeyboardButton("⚡ Speed"), KeyboardButton("⚙ Settings")],
        [KeyboardButton("👥 Manage Users")]
    ],
    resize_keyboard=True
)

USER_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Upload Bot"), KeyboardButton("📂 My Bots")],
        [KeyboardButton("▶ Start Bot"), KeyboardButton("⏹ Stop Bot"), KeyboardButton("🔄 Restart Bot")],
        [KeyboardButton("📜 Logs"), KeyboardButton("⚡ Speed")],
        [KeyboardButton("👤 Profile")]
    ],
    resize_keyboard=True
)

def get_main_keyboard(user_id):
    return ADMIN_KEYBOARD if user_id == OWNER_ID else USER_KEYBOARD

def get_bots_keyboard(user_id):
    conn = get_db_connection()
    if user_id == OWNER_ID:
        bots = conn.execute("SELECT * FROM bots").fetchall()
    else:
        bots = conn.execute("SELECT * FROM bots WHERE uploaded_by = ?", (user_id,)).fetchall()
    conn.close()
    
    buttons = []
    for bot in bots:
        is_running = is_process_running(bot["pid"])
        status_icon = "🟢 ACTIVE" if is_running else "🔴 STOPPED"
        uploader_suffix = f" (by {bot['uploaded_by'] or 'Admin'})" if user_id == OWNER_ID else ""
        buttons.append([
            InlineKeyboardButton(f"🤖 {bot['name']}{uploader_suffix} ({status_icon})", callback_data=f"manage_{bot['id']}")
        ])
    buttons.append([InlineKeyboardButton("❌ Close Menu", callback_data="close_menu")])
    return InlineKeyboardMarkup(buttons)

def get_action_keyboard(action, user_id):
    conn = get_db_connection()
    if user_id == OWNER_ID:
        bots = conn.execute("SELECT * FROM bots").fetchall()
    else:
        bots = conn.execute("SELECT * FROM bots WHERE uploaded_by = ?", (user_id,)).fetchall()
    conn.close()
    
    buttons = []
    for bot in bots:
        is_running = is_process_running(bot["pid"])
        status_icon = "🟢 ACTIVE" if is_running else "🔴 STOPPED"
        uploader_suffix = f" (by {bot['uploaded_by'] or 'Admin'})" if user_id == OWNER_ID else ""
        buttons.append([
            InlineKeyboardButton(f"🤖 {bot['name']}{uploader_suffix} ({status_icon})", callback_data=f"{action}_{bot['id']}")
        ])
    buttons.append([InlineKeyboardButton("❌ Close Menu", callback_data="close_menu")])
    return InlineKeyboardMarkup(buttons)

def get_bot_control_keyboard(bot_id, broadcast_enabled=False, is_admin=False):
    broadcast_icon = "📢 Broadcast: ON" if broadcast_enabled else "📢 Broadcast: OFF"
    buttons = [
        [
            InlineKeyboardButton("▶ Start", callback_data=f"start_{bot_id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{bot_id}"),
            InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{bot_id}")
        ],
        [
            InlineKeyboardButton("📜 View Logs", callback_data=f"logs_{bot_id}"),
            InlineKeyboardButton("📁 File Manager", callback_data=f"fm_{bot_id}")
        ],
        [
            InlineKeyboardButton("✏️ Rename Bot", callback_data=f"rename_bot_{bot_id}")
        ]
    ]
    if is_admin:
        buttons.append([
            InlineKeyboardButton(broadcast_icon, callback_data=f"toggle_bc_{bot_id}"),
            InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_confirm_{bot_id}")
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_confirm_{bot_id}")
        ])
    buttons.append([
        InlineKeyboardButton("🔙 Back to List", callback_data="back_to_list")
    ])
    return InlineKeyboardMarkup(buttons)

def get_recent_bot_logs(bot_id, lines_count=15):
    import html
    log_file_path = os.path.join("logs", f"bot_{bot_id}.log")
    if os.path.exists(log_file_path):
        try:
            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                lines = lf.readlines()
                raw_logs = "".join(lines[-lines_count:])
                return html.escape(raw_logs)
        except Exception as e:
            return f"Error reading logs: {html.escape(str(e))}"
    return "No logs available (log file not found)."

# Auto-Restart Job (runs in background)
async def auto_restart_daemon(bot):
    while True:
        try:
            conn = get_db_connection()
            running_bots = conn.execute("SELECT * FROM bots WHERE status = 'Running'").fetchall()
            conn.close()
            
            for bot_row in running_bots:
                bot_data = dict(bot_row)
                bot_id = bot_data["id"]
                pid = bot_data["pid"]
                
                if not is_process_running(pid):
                    logger.warning(f"Bot {bot_data['name']} (ID: {bot_id}) is marked 'Running' but process is dead. Checking auto-restart...")
                    # Automatically backup databases on crash/stop detection
                    backup_bot_databases(bot_id)
                    
                    crash_traceback = get_recent_bot_logs(bot_id, 10)
                    restart_count = bot_data["restart_count"]
                    if restart_count < 5:
                        new_count = restart_count + 1
                        logger.info(f"Auto-restarting bot {bot_data['name']} (Attempt {new_count}/5)...")
                        
                        conn = get_db_connection()
                        conn.execute("UPDATE bots SET restart_count = ? WHERE id = ?", (new_count, bot_id))
                        conn.commit()
                        conn.close()
                        
                        success, detail = await start_bot_process(bot_id, run_install=False, reset_restart_count=False)
                        if success:
                            log_msg = (
                                f"🔄 <b>[Auto-Restart]</b>\n\n"
                                f"Bot <b>{bot_data['name']}</b> crashed and was automatically restarted.\n"
                                f"Attempt: <code>{new_count}/5</code>\n"
                                f"New PID: <code>{detail}</code>\n\n"
                                f"📝 <b>Recent logs:</b>\n"
                                f"<pre>{crash_traceback}</pre>"
                            )
                        else:
                            log_msg = (
                                f"⚠️ <b>[Auto-Restart Failed]</b>\n\n"
                                f"Bot <b>{bot_data['name']}</b> crashed and failed to auto-restart.\n"
                                f"Attempt: <code>{new_count}/5</code>\n"
                                f"Error: <code>{detail}</code>\n\n"
                                f"📝 <b>Recent logs:</b>\n"
                                f"<pre>{crash_traceback}</pre>"
                            )
                            
                        try:
                            await bot.send_message(chat_id=OWNER_ID, text=log_msg, parse_mode="HTML")
                        except Exception as ne:
                            logger.error(f"Failed to notify owner: {ne}")
                        
                        uploaded_by = bot_data.get("uploaded_by")
                        if uploaded_by and int(uploaded_by) != OWNER_ID:
                            try:
                                await bot.send_message(chat_id=int(uploaded_by), text=log_msg, parse_mode="HTML")
                            except Exception as ne:
                                logger.error(f"Failed to notify uploader {uploaded_by}: {ne}")
                    else:
                        logger.error(f"Bot {bot_data['name']} exceeded max auto-restart attempts (5). Setting status to Stopped.")
                        conn = get_db_connection()
                        conn.execute("UPDATE bots SET status = 'Stopped', pid = NULL WHERE id = ?", (bot_id,))
                        conn.commit()
                        conn.close()
                        
                        crash_traceback_full = get_recent_bot_logs(bot_id, 15)
                        try:
                            await bot.send_message(
                                chat_id=OWNER_ID,
                                text=(
                                    f"🚨 <b>[Critical Bot Crash]</b>\n\n"
                                    f"Bot <b>{bot_data['name']}</b> has crashed repeatedly (exceeded 5 restart attempts).\n"
                                    f"Status has been set to <b>Stopped</b>.\n\n"
                                    f"📝 <b>Recent Crash Logs:</b>\n"
                                    f"<pre>{crash_traceback_full}</pre>"
                                ),
                                parse_mode="HTML"
                            )
                        except Exception as ne:
                            logger.error(f"Failed to notify owner: {ne}")
                            
                        uploaded_by = bot_data.get("uploaded_by")
                        if uploaded_by and int(uploaded_by) != OWNER_ID:
                            try:
                                await bot.send_message(
                                    chat_id=int(uploaded_by),
                                    text=(
                                        f"🚨 <b>[Critical Bot Crash]</b>\n\n"
                                        f"Your bot <b>{bot_data['name']}</b> has crashed repeatedly (exceeded 5 restart attempts).\n"
                                        f"Status has been set to <b>Stopped</b>.\n\n"
                                        f"📝 <b>Recent Crash Logs:</b>\n"
                                        f"<pre>{crash_traceback_full}</pre>"
                                    ),
                                    parse_mode="HTML"
                                )
                            except Exception as ne:
                                logger.error(f"Failed to notify uploader {uploaded_by}: {ne}")
                else:
                    try:
                        p = psutil.Process(pid)
                        children = p.children(recursive=True)
                        
                        total_ram = p.memory_info().rss
                        for child in children:
                            try:
                                total_ram += child.memory_info().rss
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                        
                        total_cpu = p.cpu_percent(interval=None)
                        for child in children:
                            try:
                                total_cpu += child.cpu_percent(interval=None)
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                                
                        folder = bot_data["folder"]
                        total_storage = get_folder_size(folder)
                        
                        ram_limit = 300 * 1024 * 1024  # 300 MB per bot (HF Spaces)
                        storage_limit = 50 * 1024 * 1024
                        
                        overloaded = False
                        reason = ""
                        
                        if total_ram > ram_limit:
                            overloaded = True
                            reason = f"RAM limit exceeded (Used: {format_size(total_ram)} / Limit: 300 MB)"
                        elif total_storage > storage_limit:
                            overloaded = True
                            reason = f"Storage limit exceeded (Used: {format_size(total_storage)} / Limit: 50 MB)"
                        else:
                            if total_cpu > 80.0:
                                BOT_CPU_SPIKES[bot_id] = BOT_CPU_SPIKES.get(bot_id, 0) + 1
                                if BOT_CPU_SPIKES[bot_id] >= 3:
                                    overloaded = True
                                    reason = f"CPU hogging detected (CPU: {total_cpu:.1f}% for 30+ seconds)"
                            else:
                                BOT_CPU_SPIKES[bot_id] = 0
                                
                        if overloaded:
                            logger.warning(f"Resource protection triggered: Kill bot {bot_data['name']} (ID: {bot_id}) - {reason}")
                            
                            for child in children:
                                try:
                                    child.kill()
                                except Exception:
                                    pass
                            try:
                                p.kill()
                            except Exception:
                                pass
                                
                            conn = get_db_connection()
                            conn.execute(
                                "UPDATE bots SET status = 'STOPPED (Resource Overload)', pid = NULL WHERE id = ?",
                                (bot_id,)
                            )
                            conn.commit()
                            conn.close()
                            
                            log_msg = (
                                f"⚠️ <b>[Resource Limit Triggered]</b>\n\n"
                                f"Bot <b>{bot_data['name']}</b> has been automatically stopped.\n"
                                f"Reason: <code>{reason}</code>\n\n"
                                f"💡 <i>Please optimize your loops, thread counts, or writing logs in your script before starting again.</i>"
                            )
                            
                            try:
                                await bot.send_message(
                                    chat_id=OWNER_ID,
                                    text=(
                                        f"🚨 <b>Admin Resource Protection Alert</b>\n\n"
                                        f"Bot <b>{bot_data['name']}</b> (ID: {bot_id}) uploaded by "
                                        f"<code>{bot_data.get('uploaded_by') or 'Admin'}</code> has been terminated.\n"
                                        f"Reason: <code>{reason}</code>"
                                    ),
                                    parse_mode="HTML"
                                )
                            except Exception as ne:
                                logger.error(f"Failed to notify owner of resource trigger: {ne}")
                                
                            uploaded_by = bot_data.get("uploaded_by")
                            if uploaded_by and int(uploaded_by) != OWNER_ID:
                                try:
                                    await bot.send_message(chat_id=int(uploaded_by), text=log_msg, parse_mode="HTML")
                                except Exception as ne:
                                    logger.error(f"Failed to notify uploader {uploaded_by}: {ne}")
                                    
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except Exception as e:
            import traceback
            logger.error(f"Error in auto-restart daemon: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(10)

async def show_users_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id != OWNER_ID:
        if update.message:
            await update.message.reply_text("⛔ <b>Admin only command!</b>", parse_mode="HTML")
        elif update.callback_query:
            try:
                await update.callback_query.answer("⛔ Admin Only", show_alert=True)
            except Exception:
                pass
        return

    conn = get_db_connection()
    users = conn.execute("SELECT user_id, allowed_at, max_bots FROM allowed_users ORDER BY allowed_at DESC").fetchall()
    
    if not users:
        users_text = (
            "👥 <b>USER PERMISSION MANAGER</b> 👥\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "No extra users are currently authorized to use this Hosting Bot.\n\n"
            "👇 <b>Actions</b>:\n"
            "• Press <b>➕ Add User</b> to authorize a new user ID."
        )
    else:
        users_text = (
            "👥 <b>USER PERMISSION MANAGER</b> 👥\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Here is the list of authorized users and their slots limit:\n\n"
        )
        for idx, u in enumerate(users, 1):
            limit = u['max_bots'] if u['max_bots'] is not None else 1
            # Query bots uploaded by this user
            user_bots = conn.execute("SELECT name, status, pid FROM bots WHERE uploaded_by = ?", (u['user_id'],)).fetchall()
            users_text += f"{idx}. User ID: <code>{u['user_id']}</code> (Slots: <b>{len(user_bots)}/{limit}</b>)\n"
            if not user_bots:
                users_text += "   └ <i>No bots uploaded yet.</i>\n"
            else:
                for b in user_bots:
                    is_running = is_process_running(b["pid"])
                    status_icon = "🟢 ACTIVE" if is_running else "🔴 STOPPED"
                    users_text += f"   ├ 🤖 {b['name']} ({status_icon})\n"
            users_text += "\n"
        
        users_text += (
            "👇 <b>Actions</b>:\n"
            "• Press <b>➕ Add User</b> to authorize a new user ID.\n"
            "• Click on any user button below to manage their bot slots and access."
        )
    conn.close()
    
    buttons = [
        [
            InlineKeyboardButton("➕ Add User", callback_data="user_add_prompt"),
            InlineKeyboardButton("📋 All Bots", callback_data="admin_all_bots")
        ]
    ]
    for u in users:
        buttons.append([
            InlineKeyboardButton(f"👤 Manage ID: {u['user_id']}", callback_data=f"user_manage_{u['user_id']}")
        ])
    buttons.append([InlineKeyboardButton("❌ Close Menu", callback_data="close_menu")])
    
    try:
        if update.message:
            await update.message.reply_text(
                users_text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
        elif update.callback_query:
            await update.callback_query.message.edit_text(
                users_text,
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to show users manager: {e}")

async def show_single_user_manager(query, target_user_id, context):
    conn = get_db_connection()
    allowed_user = conn.execute("SELECT * FROM allowed_users WHERE user_id = ?", (target_user_id,)).fetchone()
    if not allowed_user:
        await query.answer("User not found or access already revoked.", show_alert=True)
        # Go back to manager
        class FakeUpdate:
            def __init__(self, q):
                self.callback_query = q
                self.message = None
        await show_users_manager(FakeUpdate(query), context)
        return
        
    allowed_user = dict(allowed_user)
    max_bots = allowed_user.get("max_bots", 1)
    if max_bots is None:
        max_bots = 1
        
    # Get user referred count
    ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (target_user_id,)).fetchone()[0]
    
    # Get referred users details
    referred_rows = conn.execute("SELECT referred_id, created_at FROM referrals WHERE referrer_id = ?", (target_user_id,)).fetchall()
    referred_list = []
    for r in referred_rows:
        referred_list.append(f"<code>{r['referred_id']}</code> (at {r['created_at']})")
        
    # Get user bots
    user_bots = conn.execute("SELECT name, status, pid FROM bots WHERE uploaded_by = ?", (target_user_id,)).fetchall()
    conn.close()
    
    user_details = (
        "👤 <b>USER DETAILS & ACCESS CONTROL</b> 👤\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>User ID</b>: <code>{target_user_id}</code>\n"
        f"📅 <b>Authorized At</b>: <code>{allowed_user['allowed_at']}</code>\n\n"
        f"🤖 <b>Bot Slots Limit</b>: <code>{max_bots}</code> bot(s)\n"
        f"📦 <b>Bots Hosted</b>: <code>{len(user_bots)}</code>\n"
    )
    if user_bots:
        for idx, b in enumerate(user_bots, 1):
            is_running = is_process_running(b["pid"])
            status_icon = "🟢 ACTIVE" if is_running else "🔴 STOPPED"
            user_details += f"  ├ {idx}. 🤖 {b['name']} ({status_icon})\n"
            
    user_details += (
        f"\n👥 <b>Referrals Made</b>: <code>{ref_count}</code>\n"
    )
    if referred_list:
        for idx, ref_info in enumerate(referred_list, 1):
            user_details += f"  ├ {idx}. {ref_info}\n"
    else:
        user_details += "  └ <i>No referrals recorded.</i>\n"
        
    user_details += (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Actions</b>:\n"
        "• Click <b>➕ Add Slot</b> to increase the bot slot limit.\n"
        "• Click <b>➖ Remove Slot</b> to decrease the bot slot limit.\n"
        "• Click <b>🗑 Revoke Access</b> to disable hosting permission."
    )
    
    buttons = [
        [
            InlineKeyboardButton("➕ Add Slot", callback_data=f"user_addslot_{target_user_id}"),
            InlineKeyboardButton("➖ Remove Slot", callback_data=f"user_removeslot_{target_user_id}")
        ],
        [
            InlineKeyboardButton("🗑 Revoke Access", callback_data=f"user_revoke_{target_user_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back to User Manager", callback_data="user_manager_back")
        ]
    ]
    
    await query.message.edit_text(
        user_details,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML"
    )
    await query.answer()

# Security check decorator
def owner_only_check(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        # Owner is always allowed
        if user.id == OWNER_ID:
            return await func(update, context)
            
        # Allow basic public commands to let users see their profile and refer others
        is_public = False
        if update.message and update.message.text:
            text = update.message.text.strip()
            if text.startswith("/start") or text == "👤 Profile" or text == "⚡ Speed" or text == "❌ Cancel Upload":
                is_public = True
                
        if is_public:
            return await func(update, context)
            
        # Check if user is authorized in database (allowed_users)
        conn = get_db_connection()
        allowed = conn.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (int(user.id),)).fetchone()
        ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (int(user.id),)).fetchone()[0]
        conn.close()
        
        # Auto-unlock if user has 3 referrals but is not yet in allowed_users
        if not allowed and ref_count >= 3:
            conn = get_db_connection()
            conn.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (int(user.id),))
            conn.commit()
            conn.close()
            allowed = True
            
        if not allowed:
            ref_link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
            access_denied_msg = (
                "⛔ <b>VPS Hosting Locked!</b>\n\n"
                "You need to refer 3 users to unlock VPS hosting slots.\n\n"
                f"👥 <b>Your Referrals</b>: <code>{ref_count}/3</code>\n"
                f"🔗 <b>Your Referral Link</b>: <code>{ref_link}</code>\n\n"
                "Share this link with your friends. Once 3 users start the bot via your link, your slots will automatically unlock!"
            )
            if update.message:
                await update.message.reply_text(access_denied_msg, reply_markup=USER_KEYBOARD, parse_mode="HTML")
            elif update.callback_query:
                try:
                    await update.callback_query.answer("⛔ VPS Hosting Locked. Need 3 referrals.", show_alert=True)
                except Exception:
                    pass
            return
            
        return await func(update, context)
    return wrapper

@owner_only_check
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = get_db_connection()
    is_new_user = False
    user_row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user_row:
        is_new_user = True
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    conn.close()
    
    # Process referral if present and it's a new user
    if is_new_user and context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
                if referrer_id != user_id:
                    conn = get_db_connection()
                    # Check if already referred
                    already_referred = conn.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (user_id,)).fetchone()
                    if not already_referred:
                        conn.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, user_id))
                        conn.commit()
                        
                        # Count referrals of referrer
                        ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,)).fetchone()[0]
                        if ref_count >= 3:
                            # Automatically unlock referrer
                            conn.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (referrer_id,))
                            conn.commit()
                            
                            # Notify referrer of unlock
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        "🎉 <b>VPS Hosting Unlocked!</b>\n\n"
                                        "You have successfully referred 3 users. Your VPS hosting slots are now unlocked!\n"
                                        "Use the 📤 <b>Upload Bot</b> button in the main menu to host your bot."
                                    ),
                                    parse_mode="HTML"
                                )
                            except Exception as ne:
                                logger.error(f"Failed to notify referrer: {ne}")
                        else:
                            # Notify referrer of progress
                            try:
                                await context.bot.send_message(
                                    chat_id=referrer_id,
                                    text=(
                                        f"👤 <b>New Referral Registered!</b>\n\n"
                                        f"A user has started the bot via your referral link.\n"
                                        f"👥 <b>Your Referrals</b>: <code>{ref_count}/3</code>\n\n"
                                        f"💡 <i>You need {3 - ref_count} more referral(s) to unlock your VPS hosting slot.</i>"
                                    ),
                                    parse_mode="HTML"
                                )
                            except Exception as ne:
                                logger.error(f"Failed to notify referrer: {ne}")
                    conn.close()
            except Exception as e:
                logger.error(f"Error handling referral: {e}")

    # Build welcome message based on Admin vs regular User
    is_owner = (user_id == OWNER_ID)
    if is_owner:
        welcome_text = (
            "👑 <b>GAMEOVER VPS HOSTING PANEL</b> 👑\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Welcome Admin</b>: <code>{update.effective_user.first_name}</code>\n"
            f"🆔 <b>User ID</b>: <code>{user_id}</code>\n"
            f"💳 <b>Tier Plan</b>: <code>Owner (Unlimited)</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚀 <b>System Features</b>:\n"
            " ├ 🔄 <i>Auto-Restart On Crash</i> (Up to 5 attempts)\n"
            " ├ 📦 <i>Space-Saving DB Backups</i> (MD5 verified)\n"
            " ├ 📁 <i>Interactive File Manager</i>\n"
            " └ 📊 <i>Real-time Stats & CPU/RAM Performance</i>\n\n"
            "👇 Use the keyboard menu below to control your bots!"
        )
    else:
        conn = get_db_connection()
        allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
        allowed = allowed_row is not None
        ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()[0]
        
        # Auto-unlock on start check
        if not allowed and ref_count >= 3:
            conn.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            allowed = True
            allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
            
        slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
        conn.close()
        
        status_text = "🟢 Unlocked" if allowed else "🔴 Locked"
        ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
        
        if allowed:
            welcome_text = (
                "🚀 <b>GAMEOVER VPS HOSTING PANEL</b> 🚀\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Welcome User</b>: <code>{update.effective_user.first_name}</code>\n"
                f"🆔 <b>User ID</b>: <code>{user_id}</code>\n"
                f"💳 <b>Tier Plan</b>: <code>User TIER ({slots_limit} Slot(s))</code>\n"
                f"🔄 <b>Status</b>: <code>{status_text}</code>\n"
                f"👥 <b>Referrals</b>: <code>{ref_count}/3</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🚀 <b>System Features</b>:\n"
                " ├ 🔄 <i>Auto-Restart On Crash</i> (Up to 5 attempts)\n"
                " ├ 📦 <i>Space-Saving DB Backups</i>\n"
                " ├ 📁 <i>Interactive File Manager</i>\n"
                " └ ⚡ <i>High Speed Performance</i>\n\n"
                "👇 Use the keyboard menu below to get started!"
            )
        else:
            welcome_text = (
                "🚀 <b>GAMEOVER VPS HOSTING PANEL</b> 🚀\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Welcome User</b>: <code>{update.effective_user.first_name}</code>\n"
                f"🆔 <b>User ID</b>: <code>{user_id}</code>\n"
                f"💳 <b>Tier Plan</b>: <code>User TIER ({slots_limit} Slot(s))</code>\n"
                f"🔄 <b>Status</b>: <code>{status_text}</code>\n"
                f"👥 <b>Referrals</b>: <code>{ref_count}/3</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔒 <b>VPS Hosting is currently LOCKED!</b>\n"
                "To unlock your VPS hosting slot, you need to refer at least <b>3 users</b> to this bot.\n\n"
                "🔗 <b>Your Unique Referral Link</b> (Click to copy):\n"
                f"<code>{ref_link}</code>\n\n"
                "📢 <i>Share this link with your friends. Once 3 users start the bot using your link, your slot will unlock automatically!</i>"
            )

    # ── Try sending start.mp4 VIDEO first ───────────────────────────
    video_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.mp4")
    if os.path.exists(video_path):
        cached_video_id = get_setting("welcome_video_file_id")
        try:
            if cached_video_id:
                msg = await update.message.reply_video(
                    video=cached_video_id,
                    caption=welcome_text,
                    reply_markup=get_main_keyboard(user_id),
                    parse_mode="HTML"
                )
            else:
                with open(video_path, "rb") as vf:
                    msg = await update.message.reply_video(
                        video=vf,
                        caption=welcome_text,
                        reply_markup=get_main_keyboard(user_id),
                        parse_mode="HTML"
                    )
                    if msg and msg.video:
                        save_setting("welcome_video_file_id", msg.video.file_id)
            return
        except Exception as ve:
            logger.warning(f"Failed to send welcome video: {ve}")

    # ── Fallback: try cached photo file_id ───────────────────────────
    cached_file_id = get_setting("welcome_photo_file_id")
    if cached_file_id:
        try:
            await update.message.reply_photo(
                photo=cached_file_id,
                caption=welcome_text,
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.warning(f"Failed to send welcome photo via cached file ID: {e}")

    # ── Fallback to local welcome.png ────────────────────────────────
    image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.png")
    if os.path.exists(image_path):
        try:
            with open(image_path, "rb") as photo:
                msg = await update.message.reply_photo(
                    photo=photo,
                    caption=welcome_text,
                    reply_markup=get_main_keyboard(user_id),
                    parse_mode="HTML"
                )
                if msg and msg.photo:
                    save_setting("welcome_photo_file_id", msg.photo[-1].file_id)
            return
        except Exception as e:
            logger.error(f"Failed to send welcome banner photo: {e}")
            
    await update.message.reply_text(welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")

async def download_file_from_url(url, dest_path, max_size_mb=100):
    import httpx
    headers = {"User-Agent": "HostingBotDownloader/1.0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            try:
                head_resp = await client.head(url, headers=headers)
                if head_resp.status_code == 200:
                    cl = head_resp.headers.get("Content-Length")
                    if cl:
                        size = int(cl)
                        if size > max_size_mb * 1024 * 1024:
                            return False, f"File size exceeds the limit of {max_size_mb}MB"
            except Exception:
                pass
                
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    return False, f"HTTP error {response.status_code}"
                
                size_downloaded = 0
                with open(dest_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        size_downloaded += len(chunk)
                        if size_downloaded > max_size_mb * 1024 * 1024:
                            return False, f"File size exceeds the limit of {max_size_mb}MB"
                        f.write(chunk)
                        
            return True, size_downloaded
    except Exception as e:
        return False, str(e)

@owner_only_check
async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    # Auto-cancel pending states when clicking a main menu button
    main_buttons = (
        "📤 Upload Bot", "📂 My Bots", 
        "▶ Start Bot", "⏹ Stop Bot", "🔄 Restart Bot", 
        "📊 Statistics", "📊 Stats", "📜 Logs", 
        "👤 Profile", "⚡ Speed", "⚙ Settings", "👥 Manage Users"
    )
    if text in main_buttons:
        if user_id in USER_STATES:
            state_info = USER_STATES.pop(user_id)
            if "temp_dir" in state_info and os.path.exists(state_info["temp_dir"]):
                try:
                    shutil.rmtree(state_info["temp_dir"])
                except Exception:
                    pass
    
    # Check Cancel Upload first (global state cancel)
    if text == "❌ Cancel Upload":
        if user_id in USER_STATES:
            state_info = USER_STATES.pop(user_id)
            if "temp_dir" in state_info and os.path.exists(state_info["temp_dir"]):
                try:
                    shutil.rmtree(state_info["temp_dir"])
                except Exception:
                    pass
        await update.message.reply_text("❌ Upload and registration process cancelled.", reply_markup=get_main_keyboard(user_id))
        return
        
    # State check
    if user_id in USER_STATES:
        state_info = USER_STATES[user_id]
        state = state_info["state"]
        
        if state == "waiting_for_name":
            state_info["name"] = text
            state_info["state"] = "waiting_for_token"
            await update.message.reply_text(
                "🔑 <b>Step 2: Enter Bot Token</b>\n\nSend the bot token from @BotFather (e.g. <code>123456789:ABCdef...</code>):",
                parse_mode="HTML"
            )
            return
            
        elif state == "waiting_for_token":
            if ":" not in text:
                await update.message.reply_text(
                    "❌ <b>Invalid Bot Token format.</b> Please send a valid Telegram bot token:",
                    parse_mode="HTML"
                )
                return
            state_info["token"] = text
            state_info["state"] = "waiting_for_startup_file"
            
            temp_dir = state_info["temp_dir"]
            files = os.listdir(temp_dir)
            py_files = [f for f in files if f.endswith(".py")]
            suggested = py_files[0] if py_files else "main.py"
            
            await update.message.reply_text(
                f"📄 <b>Step 3: Enter Startup File</b>\n\nSpecify which Python file to run when starting the bot (e.g., <code>main.py</code>, <code>bot.py</code>).\n\n"
                f"💡 Suggested: <code>{suggested}</code>",
                parse_mode="HTML"
            )
            return
            
        elif state == "waiting_for_startup_file":
            startup_file = text.strip()
            temp_dir = state_info["temp_dir"]
            
            if not os.path.exists(os.path.join(temp_dir, startup_file)):
                await update.message.reply_text(
                    f"⚠️ <code>{startup_file}</code> not found in the uploaded files. Please enter a valid filename:",
                    parse_mode="HTML"
                )
                return
                
            bot_name = state_info["name"]
            bot_token = state_info["token"]
            
            await update.message.reply_text(
                "⚡ <b>Registering bot on server, please wait...</b>",
                parse_mode="HTML"
            )
            success, err, final_folder = await register_new_bot(bot_name, bot_token, temp_dir, startup_file, user_id, update=update)
            USER_STATES.pop(user_id, None)
            
            if success:
                success_text = (
                    "🎉 <b>Bot Registered Successfully!</b>\n\n"
                    f"🤖 <b>Bot Name</b>: <code>{bot_name}</code>\n"
                    f"📂 <b>Folder</b>: <code>{final_folder}</code>\n"
                    f"📄 <b>Startup File</b>: <code>{startup_file}</code>\n\n"
                    "You can now manage it from the <b>📂 My Bots</b> menu."
                )
                await update.message.reply_text(
                    success_text,
                    reply_markup=get_main_keyboard(user_id),
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text(
                    f"❌ <b>Registration failed:</b> {err}",
                    reply_markup=get_main_keyboard(user_id),
                    parse_mode="HTML"
                )
            return

        elif state == "waiting_for_name_edit":
            state_info["name"] = text.strip()
            state_info["state"] = "confirm_auto_registration"
            await show_auto_registration_preview(update, context, state_info)
            return
            
        elif state == "waiting_for_startup_edit":
            startup_file = text.strip()
            temp_dir = state_info["temp_dir"]
            if not os.path.exists(os.path.join(temp_dir, startup_file)):
                await update.message.reply_text(
                    f"⚠️ <code>{startup_file}</code> not found in the uploaded files. Please enter a valid filename:",
                    parse_mode="HTML"
                )
                return
            state_info["startup_file"] = startup_file
            state_info["state"] = "confirm_auto_registration"
            await show_auto_registration_preview(update, context, state_info)
            return
            
        elif state == "waiting_for_token_edit":
            if ":" not in text:
                await update.message.reply_text(
                    "❌ <b>Invalid Bot Token format.</b> Please send a valid Telegram bot token:",
                    parse_mode="HTML"
                )
                return
            token = text.strip()
            msg = await update.message.reply_text(
                "🔍 <b>Verifying token via Telegram API...</b>",
                parse_mode="HTML"
            )
            success, bot_name, bot_username = await verify_token_and_get_details(token)
            await msg.delete()
            
            state_info["token"] = token
            if success:
                state_info["name"] = bot_name
            state_info["state"] = "confirm_auto_registration"
            await show_auto_registration_preview(update, context, state_info)
            return
            
        elif state == "waiting_for_rename":
            new_name = text.strip()
            bot_id = state_info["fm_bot_id"]
            rel_file_path = state_info["fm_target_file"]
            
            conn = get_db_connection()
            bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            conn.close()
            
            old_full_path = os.path.join(bot["folder"], rel_file_path)
            new_rel_path = os.path.join(os.path.dirname(rel_file_path), new_name)
            new_full_path = os.path.join(bot["folder"], new_rel_path)
            
            if os.path.exists(old_full_path):
                try:
                    os.rename(old_full_path, new_full_path)
                    await update.message.reply_text(
                        f"✅ Renamed file to <code>{new_name}</code> successfully!",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    await update.message.reply_text(
                        f"❌ <b>Rename failed:</b> {e}",
                        parse_mode="HTML"
                    )
            else:
                await update.message.reply_text(
                    "❌ <b>Original file could not be found.</b>",
                    parse_mode="HTML"
                )
                
            USER_STATES[user_id]["state"] = None
            
            msg = await update.message.reply_text(
                "Refreshing directory listing...",
                parse_mode="HTML"
            )
            await refresh_fm_interface(msg, bot_id, user_id)
            return

        elif state == "waiting_for_bot_rename":
            new_name = text.strip()
            if not new_name:
                await update.message.reply_text("❌ Name cannot be empty. Please send a valid name:")
                return
            bot_id = state_info.get("bot_id")
            conn = get_db_connection()
            old_bot = conn.execute("SELECT name FROM bots WHERE id = ?", (bot_id,)).fetchone()
            conn.execute("UPDATE bots SET name = ? WHERE id = ?", (new_name, bot_id))
            conn.commit()
            conn.close()
            USER_STATES.pop(user_id, None)
            await update.message.reply_text(
                f"✅ <b>Bot Renamed Successfully!</b>\n\n"
                f"Old name: <code>{old_bot['name'] if old_bot else '?'}</code>\n"
                f"New name: <code>{new_name}</code>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard(user_id)
            )
            return


        elif state == "waiting_for_mkdir":
            folder_name = text.strip()
            bot_id = state_info["fm_bot_id"]
            rel_path = state_info.get("fm_path", "")
            
            # Sanitize folder name
            folder_name = os.path.basename(folder_name)
            if not folder_name:
                await update.message.reply_text("❌ Invalid folder name. Please try again.")
                return
                
            conn = get_db_connection()
            bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            conn.close()
            
            target_dir = os.path.join(bot["folder"], rel_path, folder_name)
            try:
                os.makedirs(target_dir, exist_ok=True)
                await update.message.reply_text(
                    f"✅ Folder <code>{folder_name}</code> created successfully!",
                    parse_mode="HTML"
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to create folder: {e}")
                
            USER_STATES[user_id]["state"] = None
            msg = await update.message.reply_text(
                "Refreshing directory listing...",
                parse_mode="HTML"
            )
            await refresh_fm_interface(msg, bot_id, user_id)
            return
            
        elif state == "waiting_for_file":
            text_strip = text.strip()
            if text_strip.startswith(("http://", "https://")):
                import urllib.parse
                parsed_url = urllib.parse.urlparse(text_strip)
                filename = os.path.basename(parsed_url.path)
                if not filename or "." not in filename:
                    filename = "bot_archive.zip"
                    
                filename_lower = filename.lower()
                if not (filename_lower.endswith(".py") or filename_lower.endswith(".zip")):
                    filename = filename + ".zip"
                    
                # Enforce slot limits for regular users
                if user_id != OWNER_ID:
                    conn = get_db_connection()
                    allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
                    slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
                    bot_count = conn.execute("SELECT COUNT(*) FROM bots WHERE uploaded_by = ?", (user_id,)).fetchone()[0]
                    conn.close()
                    if bot_count >= slots_limit:
                        await update.message.reply_text(
                            f"❌ <b>Slot Limit Reached!</b>\n\n"
                            f"You have used <b>{bot_count}/{slots_limit}</b> bot slots.\n"
                            "If you want to upload a new bot, please delete an existing bot or ask the Admin to increase your slot limit.",
                            parse_mode="HTML"
                        )
                        USER_STATES.pop(user_id, None)
                        return
                        
                msg = await update.message.reply_text(
                    f"📥 <b>[1/5] Downloading code files from link: <code>{filename}</code>...</b>",
                    parse_mode="HTML"
                )
                
                temp_dir = os.path.join("bots", f"temp_{user_id}_{int(time.time())}")
                os.makedirs(temp_dir, exist_ok=True)
                file_path = os.path.join(temp_dir, filename)
                
                success, err_or_size = await download_file_from_url(text_strip, file_path, max_size_mb=100)
                if not success:
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir)
                    await msg.edit_text(
                        f"❌ <b>Download failed:</b> {err_or_size}",
                        parse_mode="HTML"
                    )
                    return
                    
                await asyncio.sleep(0.8)
                await msg.edit_text(
                    "⚙️ <b>[2/5] Creating secure workspace environment...</b>",
                    parse_mode="HTML"
                )
                await asyncio.sleep(0.8)
                
                if filename.endswith(".zip"):
                    await msg.edit_text(
                        "📦 <b>[3/5] Extracting application archives...</b>",
                        parse_mode="HTML"
                    )
                    try:
                        def unzip_file(zip_path, extract_path):
                            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                zip_ref.extractall(extract_path)
                            os.remove(zip_path)
                        await asyncio.to_thread(unzip_file, file_path, temp_dir)
                    except Exception as e:
                        shutil.rmtree(temp_dir)
                        await msg.edit_text(
                            f"❌ <b>Extraction failed:</b> {e}",
                            parse_mode="HTML"
                        )
                        return
                else:
                    await msg.edit_text(
                        "📄 <b>[3/5] Storing python script...</b>",
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.5)
                    
                files = []
                for r, d, f_names in os.walk(temp_dir):
                    for f in f_names:
                        if f.endswith(".py"):
                            files.append(f)
                            
                if not files:
                    shutil.rmtree(temp_dir)
                    await msg.edit_text(
                        "❌ <b>Verification Error:</b> No Python files (<code>.py</code>) found in the upload.",
                        parse_mode="HTML"
                    )
                    USER_STATES.pop(user_id, None)
                    return
                    
                await asyncio.sleep(0.8)
                await msg.edit_text(
                    "🔍 <b>[4/5] Scanning codebase for credentials and tokens...</b>",
                    parse_mode="HTML"
                )
                
                token = find_token_in_directory(temp_dir)
                startup_file = find_startup_file(temp_dir)
                
                await asyncio.sleep(0.8)
                
                if token:
                    await msg.edit_text(
                        "🛰️ <b>[5/5] Connecting to Telegram API to verify bot credentials...</b>",
                        parse_mode="HTML"
                    )
                    success, bot_name, bot_username = await verify_token_and_get_details(token)
                    await asyncio.sleep(0.8)
                    if success:
                        USER_STATES[user_id] = {
                            "state": "confirm_auto_registration",
                            "temp_dir": temp_dir,
                            "name": bot_name,
                            "token": token,
                            "startup_file": startup_file,
                            "uploaded_filename": filename
                        }
                        masked_token = token[:9] + "..." + token[-4:]
                        confirm_text = (
                            "✨ <b>AUTOMATIC BOT DETECTION SUCCESS</b> ✨\n\n"
                            f"🤖 <b>Bot Name</b>: <code>{bot_name}</code>\n"
                            f"👤 <b>Username</b>: @{bot_username}\n"
                            f"🔑 <b>Token</b>: <code>{masked_token}</code>\n"
                            f"📄 <b>Startup File</b>: <code>{startup_file}</code>\n\n"
                            "We found these details in your uploaded code! Would you like to register this bot?"
                        )
                        buttons = [
                            [
                                InlineKeyboardButton("✅ Confirm & Register", callback_data="reg_confirm"),
                                InlineKeyboardButton("❌ Cancel", callback_data="reg_cancel")
                            ],
                            [
                                InlineKeyboardButton("📝 Edit Name", callback_data="reg_edit_name"),
                                InlineKeyboardButton("📄 Edit Startup File", callback_data="reg_edit_startup")
                            ],
                            [
                                InlineKeyboardButton("🔑 Edit Token", callback_data="reg_edit_token")
                            ]
                        ]
                        await msg.edit_text(confirm_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
                    else:
                        masked_token = token[:9] + "..." + token[-4:]
                        USER_STATES[user_id] = {
                            "state": "waiting_for_token",
                            "temp_dir": temp_dir,
                            "name": filename.replace(".py", "").replace(".zip", ""),
                            "uploaded_filename": filename
                        }
                        await msg.edit_text(
                            f"⚠️ <b>Token Verification Failed!</b>\n\n"
                            f"We detected a token: <code>{masked_token}</code> but it failed to verify (Error: <code>{bot_name}</code>).\n\n"
                            "Please enter a valid Telegram Bot Token manually:",
                            parse_mode="HTML"
                        )
                else:
                    USER_STATES[user_id] = {
                        "state": "waiting_for_name",
                        "temp_dir": temp_dir,
                        "uploaded_filename": filename
                    }
                    await msg.edit_text(
                        "📂 <b>File Uploaded Successfully!</b>\n\n"
                        "⚠️ <b>No Telegram Bot Token detected in files.</b>\n\n"
                        "🤖 <b>Step 1: Enter Bot Name</b>\n"
                        "Send the display name for this bot (e.g. <code>My Awesome Bot</code>):",
                        parse_mode="HTML"
                    )
            else:
                await update.message.reply_text(
                    "⚠️ <b>Invalid Input!</b>\n\n"
                    "Please upload a valid Python file (<code>.py</code>) or a ZIP archive (<code>.zip</code>), or send a direct download link (starting with http/https), or press ❌ <b>Cancel Upload</b>.",
                    parse_mode="HTML"
                )
                return
            
        elif state in ("waiting_for_fm_file_upload", "waiting_for_fm_zip_upload"):
            text_strip = text.strip()
            if text_strip.startswith(("http://", "https://")):
                is_zip_upload = (state == "waiting_for_fm_zip_upload")
                bot_id = state_info["fm_bot_id"]
                rel_path = state_info["fm_path"]
                
                conn = get_db_connection()
                bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
                conn.close()
                
                dest_dir = os.path.join(bot["folder"], rel_path)
                
                import urllib.parse
                parsed_url = urllib.parse.urlparse(text_strip)
                filename = os.path.basename(parsed_url.path)
                if not filename or "." not in filename:
                    filename = "archive.zip" if is_zip_upload else "downloaded_file"
                    
                if is_zip_upload and not filename.endswith(".zip"):
                    filename = filename + ".zip"
                    
                msg = await update.message.reply_text(
                    f"📥 <b>Downloading file from link: <code>{filename}</code>...</b>",
                    parse_mode="HTML"
                )
                
                dest_path = os.path.join(dest_dir, filename)
                
                success, err_or_size = await download_file_from_url(text_strip, dest_path, max_size_mb=100)
                if not success:
                    await msg.edit_text(
                        f"❌ <b>Download failed:</b> {err_or_size}",
                        parse_mode="HTML"
                    )
                    return
                    
                if is_zip_upload:
                    await msg.edit_text("⚙️ <b>Extracting ZIP archive...</b>", parse_mode="HTML")
                    try:
                        def unzip_file(zip_path, extract_path):
                            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                                zip_ref.extractall(extract_path)
                            os.remove(zip_path)
                        await asyncio.to_thread(unzip_file, dest_path, dest_dir)
                        
                        try:
                            await install_bot_dependencies(bot["folder"], bot["name"])
                        except Exception:
                            pass
                            
                        await msg.edit_text(
                            f"✅ Archive extracted successfully in <code>{rel_path or '/'}</code>!",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        if os.path.exists(dest_path):
                            try:
                                os.remove(dest_path)
                            except Exception:
                                pass
                        await msg.edit_text(
                            f"❌ <b>Extraction failed:</b> {e}",
                            parse_mode="HTML"
                        )
                else:
                    await msg.edit_text(
                        f"✅ File <code>{filename}</code> downloaded successfully to <code>{rel_path or '/'}</code>!",
                        parse_mode="HTML"
                    )
                    
                USER_STATES[user_id]["state"] = None
                fake_msg = await update.message.reply_text(
                    "Refreshing directory listing...",
                    parse_mode="HTML"
                )
                await refresh_fm_interface(fake_msg, bot_id, user_id)
                return
            else:
                await update.message.reply_text(
                    "⚠️ <b>Invalid Input!</b>\n\n"
                    "Please upload the requested file/archive to the current directory, or send a direct download link (starting with http/https), or use the menu below to navigate.",
                    parse_mode="HTML"
                )
                return
            
        elif state == "waiting_for_add_userid":
            # Clear state
            USER_STATES.pop(user_id, None)
            try:
                target_id = int(text.strip())
            except ValueError:
                await update.message.reply_text(
                    "❌ <b>Invalid format!</b> User ID must be a numerical value. Please try again:",
                    parse_mode="HTML",
                    reply_markup=get_main_keyboard(user_id)
                )
                return
                
            conn = get_db_connection()
            try:
                conn.execute("INSERT OR IGNORE INTO allowed_users (user_id) VALUES (?)", (target_id,))
                conn.commit()
                await update.message.reply_text(
                    f"✅ User <code>{target_id}</code> has been successfully authorized!",
                    parse_mode="HTML",
                    reply_markup=get_main_keyboard(user_id)
                )
                # Send direct notification to target user
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=(
                            "🎉 <b>Bot Unlocked Now!</b>\n\n"
                            "Your VPS hosting slot has been authorized by the Admin.\n"
                            "You can now use the 📤 <b>Upload Bot</b> button in the menu to host your bot! Enjoy!"
                        ),
                        parse_mode="HTML"
                    )
                except Exception as ne:
                    logger.warning(f"Could not notify unlocked user {target_id}: {ne}")
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to authorize user: {e}", reply_markup=get_main_keyboard(user_id))
            finally:
                conn.close()
            
            # Show manager panel again
            await show_users_manager(update, context)
            return
            
        elif state == "waiting_for_file_content_edit":
            bot_id = state_info["fm_bot_id"]
            rel_file_path = state_info["fm_target_file"]
            
            conn = get_db_connection()
            bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            conn.close()
            
            full_path = os.path.join(bot["folder"], rel_file_path)
            try:
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(text)
                await update.message.reply_text(
                    f"✅ Content of <code>{os.path.basename(rel_file_path)}</code> updated successfully!",
                    parse_mode="HTML"
                )
                
                # Check if the bot is currently running, if so, restart it
                is_running = is_process_running(bot["pid"])
                if is_running:
                    restart_msg = await update.message.reply_text(
                        "🔄 <b>Detecting bot is active. Restarting service to apply changes...</b>",
                        parse_mode="HTML"
                    )
                    stop_bot_process(bot_id)
                    await asyncio.sleep(1)
                    success, detail = await start_bot_process(bot_id, update=update)
                    if success:
                        await restart_msg.edit_text(
                            f"🟢 <b>Bot restarted automatically!</b>\nNew PID: <code>{detail}</code>",
                            parse_mode="HTML"
                        )
                    else:
                        await restart_msg.edit_text(
                            f"⚠️ <b>Auto-restart failed:</b> {detail}",
                            parse_mode="HTML"
                        )
            except Exception as e:
                await update.message.reply_text(
                    f"❌ <b>Edit failed:</b> {e}",
                    parse_mode="HTML"
                )
                
            USER_STATES[user_id]["state"] = None
            msg = await update.message.reply_text(
                "Refreshing directory listing...",
                parse_mode="HTML"
            )
            await refresh_fm_interface(msg, bot_id, user_id)
            return
    # Restrict Admin Menus
    if text in ("👥 Manage Users", "⚙ Settings", "📊 Statistics", "📊 Stats"):
        if user_id != OWNER_ID:
            await update.message.reply_text(
                "⛔ <b>Access Denied!</b>\n\n"
                "This section is only accessible to the Admin (Owner).",
                parse_mode="HTML"
            )
            return

    # Normal menu buttons
    if text == "📤 Upload Bot":
        is_owner = (user_id == OWNER_ID)
        if not is_owner:
            conn = get_db_connection()
            allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
            allowed = allowed_row is not None
            slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
            ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()[0]
            # Check bot count
            bot_count = conn.execute("SELECT COUNT(*) FROM bots WHERE uploaded_by = ?", (user_id,)).fetchone()[0]
            conn.close()
            
            if not allowed and ref_count < 3:
                ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
                await update.message.reply_text(
                    f"⛔ <b>Access Denied!</b>\n\n"
                    f"You need to refer 3 users to unlock VPS hosting slots.\n"
                    f"👥 <b>Your Referrals</b>: <code>{ref_count}/3</code>\n"
                    f"🔗 <b>Your Referral Link</b>: <code>{ref_link}</code>\n\n"
                    "Share this link with your friends. Once 3 users start the bot via your link, your slots will automatically unlock!",
                    parse_mode="HTML"
                )
                return
                
            if bot_count >= slots_limit:
                await update.message.reply_text(
                    f"❌ <b>Slot Limit Reached!</b>\n\n"
                    f"You have used <b>{bot_count}/{slots_limit}</b> bot slots.\n"
                    "If you want to upload a new bot, please delete an existing bot or ask the Admin to increase your slot limit.",
                    parse_mode="HTML"
                )
                return

        USER_STATES[user_id] = {"state": "waiting_for_file"}
        await update.message.reply_text(
            "📤 <b>Upload Bot Code</b>\n\n"
            "Please upload your bot files. You can send:\n"
            "• A single <code>.py</code> script file\n"
            "• A <code>.zip</code> archive containing all your bot files\n\n"
            "⚠️ <b>Important Note</b>: Please ensure your startup Python file (e.g. <code>main.py</code>) and a <code>requirements.txt</code> file are included in your upload.\n\n"
            "Waiting for your file upload...",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel Upload")]], resize_keyboard=True),
            parse_mode="HTML"
        )
        
    elif text == "📂 My Bots":
        await update.message.reply_text(
            "📂 <b>My Hosted Bots</b>\n\nSelect a bot from the list below to view status and manage controls:",
            reply_markup=get_bots_keyboard(user_id),
            parse_mode="HTML"
        )
        
    elif text == "▶ Start Bot":
        await update.message.reply_text(
            "▶ <b>Start Service</b>\n\nSelect a bot from the list below to start it:",
            reply_markup=get_action_keyboard("start", user_id),
            parse_mode="HTML"
        )
        
    elif text == "⏹ Stop Bot":
        await update.message.reply_text(
            "⏹ <b>Stop Service</b>\n\nSelect a bot from the list below to stop it:",
            reply_markup=get_action_keyboard("stop", user_id),
            parse_mode="HTML"
        )
        
    elif text == "🔄 Restart Bot":
        await update.message.reply_text(
            "🔄 <b>Restart Service</b>\n\nSelect a bot from the list below to restart it:",
            reply_markup=get_action_keyboard("restart", user_id),
            parse_mode="HTML"
        )
        
    elif text == "📜 Logs":
        await update.message.reply_text(
            "📜 <b>Log Viewer</b>\n\nSelect a bot from the list below to view its logs:",
            reply_markup=get_action_keyboard("logs", user_id),
            parse_mode="HTML"
        )
        
    elif text == "⚡ Speed":
        await send_speed_stats(update.message, user_id, is_callback=False)
        
    elif text in ("📊 Stats", "📊 Statistics"):
        msg = await update.message.reply_text(
            "📊 <b>Reading system statistics...</b>",
            parse_mode="HTML"
        )
        cpu_usage = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage('.')
        
        boot_time = psutil.boot_time()
        uptime_seconds = time.time() - boot_time
        days, remainder = divmod(int(uptime_seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m"
        
        conn = get_db_connection()
        total_bots = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        conn.close()
        
        running_bots = 0
        conn = get_db_connection()
        bots = conn.execute("SELECT * FROM bots").fetchall()
        conn.close()
        for bot in bots:
            if is_process_running(bot["pid"]):
                running_bots += 1
        stopped_bots = total_bots - running_bots
        
        python_version = platform.python_version()
        
        stats_text = (
            "📊 <b>SERVER STATISTICS</b> 📊\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>Administrators</b>: <code>1</code> (Owner)\n"
            f"🤖 <b>Managed Bots</b>: <code>{total_bots}</code>\n"
            f" ├ 🟢 <b>Active</b>: <code>{running_bots}</code>\n"
            f" └ 🔴 <b>Offline</b>: <code>{stopped_bots}</code>\n\n"
            f"💻 <b>CPU Usage</b>: <code>{cpu_usage}%</code>\n"
            f"🧠 <b>RAM Allocation</b>: <code>{ram.percent}%</code> ({format_size(ram.used)} / {format_size(ram.total)})\n"
            f"💽 <b>Disk Allocation</b>: <code>{disk.percent}%</code> ({format_size(disk.used)} / {format_size(disk.total)})\n"
            f"⏱ <b>System Uptime</b>: <code>{uptime_str}</code>\n"
            f"🐍 <b>Python Environment</b>: <code>v{python_version}</code>\n\n"
            "🟢 <i>All systems operational.</i>"
        )
        await msg.edit_text(stats_text, parse_mode="HTML")
        
    elif text == "👤 Profile":
        is_owner = (user_id == OWNER_ID)
        
        conn = get_db_connection()
        ref_count = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()[0]
        conn.close()
        
        if is_owner:
            conn = get_db_connection()
            total_bots = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
            bots = conn.execute("SELECT * FROM bots").fetchall()
            conn.close()
            
            running_bots = 0
            for bot in bots:
                if is_process_running(bot["pid"]):
                    running_bots += 1
            storage_bytes = get_folder_size("bots")
            
            profile_text = (
                "👤 <b>OWNER PROFILE CARD</b> 👤\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Admin User</b>: @{update.effective_user.username or 'N/A'}\n"
                f"🆔 <b>Owner ID</b>: <code>{OWNER_ID}</code>\n"
                f"💳 <b>Subscription</b>: <code>Owner TIER (Unlimited)</code>\n\n"
                f"🤖 <b>Bots Hosted</b>: <code>{total_bots}</code>\n"
                f"🟢 <b>Bots Running</b>: <code>{running_bots}</code>\n"
                f"📦 <b>Storage Consumption</b>: <code>{format_size(storage_bytes)}</code>"
            )
        else:
            conn = get_db_connection()
            user_bots_total = conn.execute("SELECT COUNT(*) FROM bots WHERE uploaded_by = ?", (user_id,)).fetchone()[0]
            user_bots_running = 0
            user_bots = conn.execute("SELECT pid, folder FROM bots WHERE uploaded_by = ?", (user_id,)).fetchall()
            allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
            slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
            conn.close()
            
            storage_bytes = 0
            for b in user_bots:
                if is_process_running(b["pid"]):
                    user_bots_running += 1
                storage_bytes += get_folder_size(b["folder"])
                
            allowed = (user_id == OWNER_ID)
            if not allowed:
                allowed = allowed_row is not None or ref_count >= 3
                
            status_text = "🟢 Unlocked" if allowed else "🔴 Locked (Need 3 Referrals to Host)"
            ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
            
            profile_text = (
                "👤 <b>USER PROFILE CARD</b> 👤\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>User</b>: @{update.effective_user.username or 'N/A'}\n"
                f"🆔 <b>User ID</b>: <code>{user_id}</code>\n"
                f"💳 <b>Subscription</b>: <code>User TIER</code>\n"
                f"🔄 <b>Status</b>: <code>{status_text}</code>\n"
                f"👥 <b>Referrals</b>: <code>{ref_count}/3</code>\n"
                f"🔗 <b>Referral Link</b>: <code>{ref_link}</code>\n\n"
                f"🤖 <b>Bots Hosted</b>: <code>{user_bots_total}/{slots_limit}</code>\n"
                f"🟢 <b>Bots Running</b>: <code>{user_bots_running}</code>\n"
                f"📦 <b>Storage Consumption</b>: <code>{format_size(storage_bytes)}</code>"
            )
        await update.message.reply_text(profile_text, parse_mode="HTML")
        
    elif text == "⚙ Settings":
        await update.message.reply_text(
            "⚙ <b>SYSTEM CONFIGURATION</b> ⚙\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>Owner ID</b>: <code>{OWNER_ID}</code>\n"
            f"🔄 <b>Auto-Restart Threshold</b>: <code>5 attempts</code>\n"
            f"⏱ <b>Polling Heartbeat</b>: <code>10 seconds</code>\n"
            f"📂 <b>Workspace Path</b>: <code>GAMEOVER Cloud Server/bots/</code>\n\n"
            "💡 <i>Configure parameters via .env file in root.</i>",
            parse_mode="HTML"
        )
        
    elif text == "👥 Manage Users":
        if user_id == OWNER_ID:
            await show_users_manager(update, context)
        else:
            await update.message.reply_text("⛔ <b>Admin only command!</b>", parse_mode="HTML")
        
    elif text == "❌ Cancel Upload":
        if user_id in USER_STATES:
            state_info = USER_STATES.pop(user_id)
            if "temp_dir" in state_info and os.path.exists(state_info["temp_dir"]):
                shutil.rmtree(state_info["temp_dir"])
        await update.message.reply_text(
            "❌ <b>Upload process cancelled.</b>",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "❓ <b>Unrecognized Command!</b>\n\n"
            "Please use the menu buttons below to navigate.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )

import re

def find_token_in_directory(directory):
    token_pattern = re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{35})\b")
    common_files = ['.env', 'config.py', 'main.py', 'bot.py', 'settings.py', 'config.json', 'config.ini']
    for file_name in common_files:
        file_path = os.path.join(directory, file_name)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    match = token_pattern.search(content)
                    if match:
                        return match.group(1)
            except Exception:
                pass
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(('.py', '.env', '.txt', '.json', '.yml', '.yaml', '.conf', '.ini', '.sh', '.bat')):
                file_path = os.path.join(root, file)
                try:
                    if os.path.getsize(file_path) > 1024 * 1024:
                        continue
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        match = token_pattern.search(content)
                        if match:
                            return match.group(1)
                except Exception:
                    pass
    return None

def find_startup_file(directory):
    for f in ['main.py', 'bot.py', 'app.py', 'index.py', 'run.py']:
        if os.path.exists(os.path.join(directory, f)):
            return f
    py_files_in_root = [f for f in os.listdir(directory) if f.endswith('.py') and os.path.isfile(os.path.join(directory, f))]
    if len(py_files_in_root) == 1:
        return py_files_in_root[0]
    if py_files_in_root:
        return py_files_in_root[0]
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                return os.path.relpath(os.path.join(root, file), directory)
    return "main.py"

async def verify_token_and_get_details(token):
    from telegram import Bot
    from telegram.request import HTTPXRequest
    try:
        # Load custom base URL if configured to bypass firewalls (e.g. on Hugging Face)
        custom_base_url = os.getenv("TELEGRAM_API_BASE_URL")
        request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
        
        if custom_base_url:
            custom_base_url = custom_base_url.rstrip("/")
            if not custom_base_url.endswith("/bot"):
                custom_base_url = custom_base_url + "/bot"
            temp_bot = Bot(token=token, base_url=custom_base_url, request=request_config)
        else:
            temp_bot = Bot(token=token, request=request_config)
            
        me = await temp_bot.get_me()
        return True, me.first_name, me.username
    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        return False, str(e), None


def extract_imports_from_py_files(folder):
    import ast
    imported_modules = set()
    for root, dirs, filenames in os.walk(folder):
        if "backups" in root:
            continue
        for file in filenames:
            if file.endswith(".py"):
                fp = os.path.join(root, file)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        node = ast.parse(f.read(), filename=fp)
                    for child in ast.walk(node):
                        if isinstance(child, ast.Import):
                            for alias in child.names:
                                imported_modules.add(alias.name.split('.')[0])
                        elif isinstance(child, ast.ImportFrom):
                            if getattr(child, 'level', 0) > 0:
                                continue
                            if child.module:
                                imported_modules.add(child.module.split('.')[0])
                except Exception:
                    pass
    return imported_modules

def is_local_module(folder, imp):
    if not imp:
        return False
    imp_clean = imp.split('.')[0]
    # Check top-level folder
    if os.path.exists(os.path.join(folder, f"{imp_clean}.py")) or os.path.isdir(os.path.join(folder, imp_clean)):
        return True
    # Walk to search recursively
    for root, dirs, files in os.walk(folder):
        if "backups" in root:
            continue
        if f"{imp_clean}.py" in files or imp_clean in dirs:
            return True
    return False

def get_external_requirements(folder):
    imports = extract_imports_from_py_files(folder)
    stdlib = set()
    if hasattr(sys, "stdlib_module_names"):
        stdlib.update(sys.stdlib_module_names)
    else:
        # Fallback standard library list
        stdlib.update([
            "os", "sys", "time", "math", "socket", "shutil", "sqlite3", "zipfile", "hashlib",
            "logging", "platform", "subprocess", "asyncio", "datetime", "json", "re", "select",
            "threading", "queue", "urllib", "http", "typing", "collections", "itertools", "functools",
            "traceback", "uuid", "base64", "hmac", "hashlib", "csv", "random", "string", "glob",
            "tempfile", "io", "pathlib", "ast", "inspect", "weakref", "pickle", "copy", "struct",
            "xml", "html", "cgi", "unittest", "timeit", "pdb", "distutils", "pkgutil", "pydoc"
        ])
    stdlib.update(sys.builtin_module_names)
    stdlib.update(["", "telegram"]) # Ignore telegram since python-telegram-bot is installed as 'telegram' but is already in requirements.txt
    
    external = {
        imp for imp in imports 
        if imp and imp not in stdlib 
        and not is_local_module(folder, imp)
    }
    
    # Map import names to standard pip package names if they differ
    mapping = {
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "telegram": "python-telegram-bot",
        "dateutil": "python-dateutil",
        "yaml": "PyYAML",
        "dotenv": "python-dotenv",
        "telethon": "telethon",
        "pyrogram": "pyrogram",
        "tgcrypto": "tgcrypto"
    }
    mapped_external = set()
    for imp in external:
        mapped_external.add(mapping.get(imp, imp))
        
    return list(mapped_external)

def parse_package_name(requirement_str):
    req = requirement_str.strip()
    if not req or req.startswith("#"):
        return None
    import re
    parts = re.split(r"==|>=|<=|>|<|!=|~=", req)
    package_name = parts[0].strip()
    if "[" in package_name:
        package_name = package_name.split("[")[0].strip()
    return package_name

def is_package_installed(package_name):
    name_norm = package_name.lower().replace("_", "-")
    try:
        import importlib.metadata
        importlib.metadata.version(name_norm)
        return True
    except Exception:
        try:
            import pkg_resources
            pkg_resources.get_distribution(name_norm)
            return True
        except Exception:
            try:
                __import__(package_name.replace("-", "_"))
                return True
            except Exception:
                return False

def parse_package_name_and_version(requirement_str):
    req = requirement_str.strip()
    if not req or req.startswith("#"):
        return None, None
    import re
    if "==" in req:
        parts = req.split("==")
        package_name = parts[0].strip()
        version = parts[1].strip().split()[0]
        if "[" in package_name:
            package_name = package_name.split("[")[0].strip()
        return package_name, version
    else:
        parts = re.split(r">=|<=|>|<|!=|~=", req)
        package_name = parts[0].strip()
        if "[" in package_name:
            package_name = package_name.split("[")[0].strip()
        return package_name, None

def parse_requires_dist(requires_dist_list):
    deps = []
    if not requires_dist_list:
        return deps
        
    sys_platform = platform.system().lower()
    is_linux = (sys_platform == 'linux')
    is_windows = (sys_platform == 'windows')
    
    for req in requires_dist_list:
        if ";" in req:
            parts = req.split(";", 1)
            dep_part = parts[0].strip()
            marker_part = parts[1].strip()
            
            # Simple check for common markers to skip optional/incompatible deps
            if "extra ==" in marker_part or "extra =" in marker_part:
                continue
            if "sys_platform == 'win32'" in marker_part and not is_windows:
                continue
            if "sys_platform == \"win32\"" in marker_part and not is_windows:
                continue
            if "platform_system == 'Windows'" in marker_part and not is_windows:
                continue
            if "platform_system == \"Windows\"" in marker_part and not is_windows:
                continue
            if "sys_platform == 'linux'" in marker_part and not is_linux:
                continue
            if "sys_platform == \"linux\"" in marker_part and not is_linux:
                continue
            if "platform_system == 'Linux'" in marker_part and not is_linux:
                continue
            if "platform_system == \"Linux\"" in marker_part and not is_linux:
                continue
        else:
            dep_part = req.strip()
            
        import re
        dep_name = re.split(r"[\s\(\>=<~!]", dep_part)[0].strip()
        if dep_name:
            deps.append(dep_name)
    return deps

def is_package_installed_locally(name, folder):
    import glob
    name_norm_dist = name.lower().replace("-", "_")
    for name_format in (name_norm_dist, name.lower().replace("_", "-")):
        dist_infos = glob.glob(os.path.join(folder, f"{name_format}-*.dist-info"))
        if dist_infos:
            return True
        egg_infos = glob.glob(os.path.join(folder, f"{name_format}-*.egg-info"))
        if egg_infos:
            return True
    return False

def is_mismatched_binary(file_path):
    if not os.path.isfile(file_path):
        return False
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".so", ".pyd", ".dll"):
        return False
        
    sys_platform = platform.system().lower()
    try:
        with open(file_path, "rb") as f:
            header = f.read(1024)
        if len(header) < 5:
            return False
            
        # ELF (Linux Shared Object) check
        if header.startswith(b"\x7fELF"):
            if sys_platform != "linux":
                return True
            elf_class = header[4]  # 1 = 32-bit, 2 = 64-bit
            is_64bit_python = (sys.maxsize > 2**32)
            if is_64bit_python and elf_class == 1:
                return True
            if not is_64bit_python and elf_class == 2:
                return True
                
        # PE (Windows DLL/PYD) check
        elif header.startswith(b"MZ"):
            if sys_platform != "windows":
                return True
            if len(header) >= 64:
                pe_offset = int.from_bytes(header[0x3c:0x40], byteorder="little")
                if pe_offset + 24 <= len(header):
                    pe_sig = header[pe_offset:pe_offset+4]
                    if pe_sig == b"PE\x00\x00":
                        machine = int.from_bytes(header[pe_offset+4:pe_offset+6], byteorder="little")
                        is_64bit_python = (sys.maxsize > 2**32)
                        # 0x14c = 32-bit x86, 0x8664 = 64-bit AMD64 (64-bit)
                        if is_64bit_python and machine == 0x14c:
                            return True
                        if not is_64bit_python and machine == 0x8664:
                            return True
    except Exception:
        pass
    return False

def cleanup_mismatched_local_packages(folder):
    import glob
    import shutil
    mismatched_files = []
    for root, dirs, files in os.walk(folder):
        if "backups" in root:
            continue
        for file in files:
            fp = os.path.join(root, file)
            if is_mismatched_binary(fp):
                mismatched_files.append(fp)
    if not mismatched_files:
        return
    logger.info(f"Found mismatched native binaries in {folder}: {mismatched_files}")
    to_remove = set()
    for fp in mismatched_files:
        rel = os.path.relpath(fp, folder)
        parts = rel.split(os.sep)
        if parts:
            top_dir = os.path.join(folder, parts[0])
            if os.path.isdir(top_dir):
                to_remove.add(top_dir)
    for dist_info_dir in glob.glob(os.path.join(folder, "*.dist-info")) + glob.glob(os.path.join(folder, "*.egg-info")):
        top_level_txt = os.path.join(dist_info_dir, "top_level.txt")
        if os.path.exists(top_level_txt):
            try:
                with open(top_level_txt, "r", encoding="utf-8") as f:
                    toplevels = [line.strip() for line in f if line.strip()]
                for tr in to_remove:
                    tr_base = os.path.basename(tr)
                    if tr_base in toplevels:
                        to_remove.add(dist_info_dir)
                        break
            except Exception:
                pass
    for item in to_remove:
        logger.info(f"Removing mismatched package directory/file: {item}")
        try:
            if os.path.isdir(item):
                shutil.rmtree(item)
            else:
                os.remove(item)
        except Exception as e:
            logger.error(f"Failed to remove {item} during mismatched cleanup: {e}")

async def download_package_from_pypi(package_name, version_spec, target_dir):
    import io
    import zipfile
    
    headers = {"User-Agent": "HostingBotDependencyInstaller/1.0"}
    data = None
    package_name_normalized = package_name.replace("_", "-")
    
    url = f"https://pypi.org/pypi/{package_name_normalized}/json"
    url_version = f"https://pypi.org/pypi/{package_name_normalized}/{version_spec}/json" if version_spec else None
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        if url_version:
            try:
                response = await client.get(url_version, headers=headers)
                if response.status_code == 200:
                    data = response.json()
            except Exception as e:
                logger.warning(f"Failed to fetch version specific PyPI data for {package_name}=={version_spec}: {e}")
        
        if not data:
            try:
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                else:
                    raise Exception(f"PyPI API returned status code {response.status_code}")
            except Exception as e:
                logger.error(f"Failed to fetch PyPI data for {package_name}: {e}")
                return False, f"Could not find package info on PyPI: {e}"

        wheels = []
        for file_info in data.get("urls", []):
            if file_info.get("packagetype") == "bdist_wheel" or file_info.get("filename", "").endswith(".whl"):
                wheels.append(file_info)
                
        if not wheels:
            current_version = data.get("info", {}).get("version")
            if current_version:
                for file_info in data.get("releases", {}).get(current_version, []):
                    if file_info.get("packagetype") == "bdist_wheel" or file_info.get("filename", "").endswith(".whl"):
                        wheels.append(file_info)
                        
        if not wheels:
            return False, f"No wheel package found for {package_name} on PyPI."
            
        best_wheel = None
        best_score = -1
        
        sys_platform = platform.system().lower()
        is_linux = (sys_platform == 'linux')
        is_windows = (sys_platform == 'windows')
        host_machine = platform.machine().lower()
        
        py_major = sys.version_info.major
        py_minor = sys.version_info.minor
        cp_tag = f"cp{py_major}{py_minor}"
        
        for whl in wheels:
            filename = whl.get("filename", "")
            if not filename.endswith(".whl"):
                continue
            
            base_name = filename[:-4]
            parts = base_name.split("-")
            if len(parts) < 3:
                continue
                
            plat_tag = parts[-1].lower()
            abi_tag = parts[-2].lower()
            py_tag = parts[-3].lower()
            
            score = 0
            
            # Check python compatibility:
            py_tags = py_tag.split(".")
            is_py_compatible = False
            for tag in py_tags:
                if tag in ("py3", "py2.py3", "any"):
                    is_py_compatible = True
                    score += 50
                elif tag == cp_tag:
                    is_py_compatible = True
                    score += 60
                elif tag.startswith("cp3"):
                    try:
                        v_minor = int(tag[3:])
                        if v_minor <= py_minor:
                            is_py_compatible = True
                            score += 30
                    except ValueError:
                        pass
                elif tag == "shared":
                    is_py_compatible = True
                    score += 10
                    
            if not is_py_compatible:
                continue
                
            # Check ABI compatibility:
            abi_tags = abi_tag.split(".")
            is_abi_compatible = False
            for tag in abi_tags:
                if tag == "none":
                    is_abi_compatible = True
                    score += 20
                elif tag == "abi3":
                    is_abi_compatible = True
                    score += 30
                elif tag == cp_tag:
                    is_abi_compatible = True
                    score += 40
                    
            if not is_abi_compatible:
                continue
                
            # Check platform compatibility:
            plat_tags = plat_tag.split(".")
            is_plat_compatible = False
            for tag in plat_tags:
                if tag == "any":
                    is_plat_compatible = True
                    score += 100
                elif "manylinux" in tag or "linux" in tag:
                    if is_linux:
                        # Check CPU architecture compatibility
                        arch_ok = True
                        if "x86_64" in tag or "amd64" in tag:
                            if host_machine not in ("x86_64", "amd64"):
                                arch_ok = False
                        elif "i686" in tag or "i386" in tag:
                            if host_machine not in ("i686", "i386"):
                                arch_ok = False
                        elif "aarch64" in tag or "arm64" in tag:
                            if host_machine not in ("aarch64", "arm64"):
                                arch_ok = False
                        elif "armv7l" in tag:
                            if host_machine != "armv7l":
                                arch_ok = False
                                
                        if arch_ok:
                            is_plat_compatible = True
                            score += 80
                elif "win" in tag:
                    if is_windows:
                        # Check CPU architecture compatibility
                        arch_ok = True
                        if "amd64" in tag or "x86_64" in tag:
                            if host_machine not in ("amd64", "x86_64"):
                                arch_ok = False
                        elif "win32" in tag or "x86" in tag or "i386" in tag or "i686" in tag:
                            if host_machine not in ("x86", "win32", "i386", "i686"):
                                arch_ok = False
                        elif "arm64" in tag:
                            if host_machine not in ("arm64", "aarch64"):
                                arch_ok = False
                                
                        if arch_ok:
                            is_plat_compatible = True
                            score += 80
                        
            if not is_plat_compatible:
                continue
                
            if score > best_score:
                best_score = score
                best_wheel = whl
                

        if not best_wheel and wheels:
            for whl in wheels:
                filename = whl.get("filename", "").lower()
                if "py3" in filename or "any" in filename:
                    best_wheel = whl
                    break

        if not best_wheel:
            return False, "Failed to resolve a compatible wheel package."

        download_url = best_wheel.get("url")
        filename = best_wheel.get("filename")
        if not download_url:
            return False, "Selected wheel has no URL."

        logger.info(f"Downloading wheel for {package_name} from {download_url}...")
        temp_wheel_name = f"temp_{int(time.time())}_{package_name.replace('-', '_')}.whl"
        temp_wheel_path = os.path.join(target_dir, temp_wheel_name)

        try:
            async with client.stream("GET", download_url, headers=headers) as response:
                if response.status_code != 200:
                    return False, f"Failed to download wheel: HTTP {response.status_code}"
                with open(temp_wheel_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
        except Exception as e:
            if os.path.exists(temp_wheel_path):
                try:
                    os.remove(temp_wheel_path)
                except Exception:
                    pass
            return False, f"Failed to stream wheel download: {e}"

        def extract_zip_file(zip_path, extract_path):
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(extract_path)

        try:
            await asyncio.to_thread(extract_zip_file, temp_wheel_path, target_dir)
            logger.info(f"Successfully extracted {filename} to {target_dir}")
            return True, data
        except Exception as e:
            return False, f"Failed to extract wheel: {e}"
        finally:
            if os.path.exists(temp_wheel_path):
                try:
                    os.remove(temp_wheel_path)
                except Exception:
                    pass


async def install_bot_dependencies(folder, bot_name):
    """
    Install all dependencies for a hosted bot.

    Priority:
      1. requirements.txt in the bot folder  ─── pip install -r (FASTEST, handles ALL types)
      2. Auto-scanned imports from .py files  ─── pip install <packages>
      3. PyPI wheel download fallback          ─── only if pip is unavailable

    Supports ALL bot types:
      - SMS bombers, YT-dl bots, Pyrogram/Telethon, aiogram, etc.
      - C-extension wheels (cryptography, tgcrypto, lxml, uvloop…)
      - .env file is preserved — never overwritten if user uploaded one
    """
    logger.info(f"[DepInstall] Starting for bot '{bot_name}' in {folder}")

    # Step 0 — clean mismatched native binaries from cross-platform uploads
    try:
        cleanup_mismatched_local_packages(folder)
    except Exception as e:
        logger.error(f"[DepInstall] Cleanup error for {bot_name}: {e}")

    req_path = os.path.join(folder, "requirements.txt")
    has_req_file = os.path.exists(req_path) and os.path.getsize(req_path) > 0
    using_scanned = False

    # Step 1 — use pip (fastest, handles C extensions, git deps, etc.) ──
    pip_available = False
    try:
        chk = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=15
        )
        pip_available = (chk.returncode == 0)
    except Exception:
        pass

    # ── Target dir: each bot gets its own lib/ folder for isolation ───────
    # This prevents package version conflicts between different users' bots.
    lib_dir = os.path.join(folder, "lib")
    os.makedirs(lib_dir, exist_ok=True)

    if pip_available:
        if has_req_file:
            logger.info(f"[DepInstall] Found requirements.txt — pip install -r into lib/ ...")
            cmd = [
                sys.executable, "-m", "pip", "install",
                "-r", req_path,
                "--target", lib_dir,
                "--upgrade",
                "--quiet",
                "--disable-pip-version-check",
                "--no-warn-script-location",
            ]
        else:
            logger.info(f"[DepInstall] No requirements.txt — scanning imports...")
            skip = {
                "python-telegram-bot", "telegram", "httpx", "psutil",
                "python-dotenv", "pip", "setuptools", "wheel", "python"
            }
            scanned = get_external_requirements(folder)
            using_scanned = True
            packages = [p for p in scanned if p.lower().replace("_", "-") not in skip]
            if not packages:
                logger.info(f"[DepInstall] No external packages for '{bot_name}'.")
                return True, "No external dependencies."
            logger.info(f"[DepInstall] Installing scanned packages into lib/: {packages}")
            cmd = [
                sys.executable, "-m", "pip", "install",
                *packages,
                "--target", lib_dir,
                "--upgrade",
                "--quiet",
                "--disable-pip-version-check",
                "--no-warn-script-location",
            ]

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True, text=True, timeout=300, cwd=folder
            )
            if result.returncode == 0:
                logger.info(f"[DepInstall] ✅ pip succeeded for '{bot_name}'")
                return True, "Success"
            err = (result.stderr or result.stdout or "Unknown pip error").strip()[-500:]
            logger.warning(f"[DepInstall] pip warnings for '{bot_name}': {err}")
            return True, f"Installed with warnings: {err}"
        except asyncio.TimeoutError:
            logger.error(f"[DepInstall] pip timed out for '{bot_name}'")
            return False, "Dependency installation timed out."
        except Exception as ex:
            logger.error(f"[DepInstall] pip exception for '{bot_name}': {ex}")
            # fall through to wheel fallback

    # Step 2 — fallback: PyPI wheel download (when pip is not available) ─
    logger.warning(f"[DepInstall] pip not available — using PyPI wheel fallback for '{bot_name}'")
    requirements_to_install = []
    if has_req_file:
        try:
            with open(req_path, "r", encoding="utf-8") as f:
                for line in f:
                    req_str = line.strip()
                    if req_str and not req_str.startswith("#"):
                        requirements_to_install.append(req_str)
        except Exception as e:
            logger.error(f"[DepInstall] Cannot read requirements.txt: {e}")

    if not requirements_to_install:
        requirements_to_install = get_external_requirements(folder)
        using_scanned = True

    skip = {
        "python-telegram-bot", "telegram", "httpx", "psutil",
        "python-dotenv", "pip", "setuptools", "wheel", "python"
    }
    processed = set()

    async def install_package_recursive(req_str, is_scanned_dep=False):
        name, ver = parse_package_name_and_version(req_str)
        if not name:
            return
        name_lower = name.lower().replace("_", "-")
        if name_lower in skip or name_lower in processed:
            return
        processed.add(name_lower)
        if is_package_installed(name_lower):
            return
        if is_package_installed_locally(name, folder):
            return
        logger.info(f"[DepInstall] Wheel-downloading: {name} ({ver or 'latest'})...")
        success, result = await download_package_from_pypi(name, ver, folder)
        if not success:
            logger.error(f"[DepInstall] Wheel failed for {name}: {result}")
            if not is_scanned_dep:
                raise Exception(f"Failed to install '{name}': {result}")
            return
        requires_dist = result.get("info", {}).get("requires_dist", [])
        for sub_dep in parse_requires_dist(requires_dist):
            await install_package_recursive(sub_dep, is_scanned_dep)

    for req in requirements_to_install:
        await install_package_recursive(req, is_scanned_dep=using_scanned)

    logger.info(f"[DepInstall] ✅ Wheel fallback done for '{bot_name}'")
    return True, "Success"


async def register_new_bot(bot_name, bot_token, temp_dir, startup_file, uploaded_by, update=None):
    try:
        if uploaded_by != OWNER_ID:
            conn = get_db_connection()
            allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (uploaded_by,)).fetchone()
            slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
            bot_count = conn.execute("SELECT COUNT(*) FROM bots WHERE uploaded_by = ?", (uploaded_by,)).fetchone()[0]
            conn.close()
            if bot_count >= slots_limit:
                if os.path.exists(temp_dir):
                    try:
                        shutil.rmtree(temp_dir)
                    except Exception:
                        pass
                return False, f"Slot limit reached! You can host a maximum of {slots_limit} bot(s).", None

        timestamp = int(time.time())
        final_folder = os.path.join("bots", f"bot_{timestamp}")
        os.makedirs(final_folder, exist_ok=True)
        if os.path.exists(temp_dir):
            for item in os.listdir(temp_dir):
                s = os.path.join(temp_dir, item)
                d = os.path.join(final_folder, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            shutil.rmtree(temp_dir)
        else:
            return False, "Temp folder not found.", None
            
        dependency_error = None
        try:
            await install_bot_dependencies(final_folder, bot_name)
        except Exception as de:
            dependency_error = str(de)
            logger.warning(f"Dependency installation failed for new bot {bot_name}: {de}")
        
        # Ensure a .env file is present with the bot token so the bot runs out of the box
        env_path = os.path.join(final_folder, ".env")
        if not os.path.exists(env_path):
            try:
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write(f"BOT_TOKEN={bot_token}\nTELEGRAM_BOT_TOKEN={bot_token}\n")
            except Exception as e:
                logger.error(f"Failed to create .env for new bot: {e}")
        else:
            # Check if it has a BOT_TOKEN or TELEGRAM_BOT_TOKEN inside, if not append it
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    env_content = f.read()
                if "BOT_TOKEN" not in env_content and "TELEGRAM_BOT_TOKEN" not in env_content:
                    with open(env_path, "a", encoding="utf-8") as f:
                        f.write(f"\nBOT_TOKEN={bot_token}\nTELEGRAM_BOT_TOKEN={bot_token}\n")
            except Exception as e:
                logger.error(f"Failed to append token to .env: {e}")
                
        conn = get_db_connection()
        cursor = conn.cursor()
        
        initial_status = 'Error' if dependency_error else 'Stopped'
        
        cursor.execute(
            "INSERT INTO bots (name, token, folder, startup_file, status, uploaded_by) VALUES (?, ?, ?, ?, ?, ?)",
            (bot_name, bot_token, final_folder, startup_file, initial_status, uploaded_by)
        )
        conn.commit()
        conn.close()
        
        if dependency_error:
            return True, f"Registered with warnings: {dependency_error}", final_folder
            
        return True, "Success", final_folder
    except Exception as e:
        logger.error(f"Error registering new bot: {e}")
        return False, str(e), None

async def show_auto_registration_preview(update, context, state_info):
    name = state_info["name"]
    token = state_info["token"]
    startup_file = state_info["startup_file"]
    masked_token = token[:9] + "..." + token[-4:] if len(token) > 13 else token
    confirm_text = (
        "✨ <b>AUTOMATIC BOT DETECTION</b> ✨\n\n"
        f"🤖 <b>Bot Name</b>: <code>{name}</code>\n"
        f"🔑 <b>Token</b>: <code>{masked_token}</code>\n"
        f"📄 <b>Startup File</b>: <code>{startup_file}</code>\n\n"
        "Please review the updated details. Ready to register?"
    )
    buttons = [
        [
            InlineKeyboardButton("✅ Confirm & Register", callback_data="reg_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reg_cancel")
        ],
        [
            InlineKeyboardButton("📝 Edit Name", callback_data="reg_edit_name"),
            InlineKeyboardButton("📄 Edit Startup File", callback_data="reg_edit_startup")
        ],
        [
            InlineKeyboardButton("🔑 Edit Token", callback_data="reg_edit_token")
        ]
    ]
    try:
        if update.message:
            await update.message.reply_text(confirm_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.message.edit_text(confirm_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to show auto-reg preview: {e}")

@owner_only_check
async def file_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    doc = update.message.document
    
    # System Backup ZIP Detection for Owner
    if user_id == OWNER_ID and doc and doc.file_name and doc.file_name.endswith(".zip") and "backup" in doc.file_name.lower():
        temp_inspect_path = f"temp_inspect_backup_{user_id}.zip"
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(temp_inspect_path)
            
            is_valid_backup = False
            try:
                with zipfile.ZipFile(temp_inspect_path, 'r') as inspect_zip:
                    namelist = inspect_zip.namelist()
                    if "hosting.py" in namelist and any("bots.db" in name for name in namelist):
                        is_valid_backup = True
            except Exception:
                pass
                
            if is_valid_backup:
                USER_STATES[user_id] = {
                    "state": "confirm_system_restore",
                    "backup_path": temp_inspect_path,
                    "message_id": update.message.message_id
                }
                
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Confirm Restore & Overwrite", callback_data=f"sys_restore_confirm_{update.message.message_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data="sys_restore_cancel")
                    ]
                ])
                await update.message.reply_text(
                    "📥 <b>SYSTEM BACKUP DETECTED!</b> 📥\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "This ZIP archive contains a complete backup of the hosting manager, uploader bots, database, and configurations.\n\n"
                    "⚠️ <b>WARNING:</b> Restoring will stop all running bots, overwrite all local files/databases, and automatically restart the hosting manager!\n\n"
                    "Are you sure you want to restore the system state from this backup?",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                return
            else:
                if os.path.exists(temp_inspect_path):
                    os.remove(temp_inspect_path)
        except Exception as e:
            logger.error(f"Error checking uploaded backup: {e}")
            if os.path.exists(temp_inspect_path):
                try:
                    os.remove(temp_inspect_path)
                except Exception:
                    pass

    # If a document is sent but it's completely out of active states, notify user nicely
    if user_id not in USER_STATES or USER_STATES[user_id].get("state") not in ("waiting_for_file", "waiting_for_fm_file_upload", "waiting_for_fm_zip_upload"):
        if user_id in USER_STATES and "fm_bot_id" in USER_STATES[user_id]:
            USER_STATES[user_id]["pending_doc"] = doc
            filename = doc.file_name
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📤 Upload to Current Folder", callback_data="fm_pending_upload"),
                    InlineKeyboardButton("🤖 Register as New Bot", callback_data="fm_pending_register")
                ],
                [
                    InlineKeyboardButton("❌ Cancel", callback_data="fm_pending_cancel")
                ]
            ])
            await update.message.reply_text(
                f"📥 <b>File Detected:</b> <code>{filename}</code>\n\n"
                "You are currently inside the File Manager. What would you like to do with this file?",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            return
            
        await update.message.reply_text(
            "⚠️ <b>No Active Upload Session!</b>\n\n"
            "If you want to upload a new bot, please press 📤 <b>Upload Bot</b> first.\n"
            "If you are using File Manager, select 📤 <b>Upload File</b> inside the File Manager first.",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )
        return

    filename = doc.file_name or ""

    # ── .env file direct upload (no active session needed) ───────────────
    # User can send a .env file at ANY time to update an existing bot's config.
    if filename == ".env" or filename.endswith(".env"):
        conn = get_db_connection()
        user_bots = conn.execute(
            "SELECT id, name, folder FROM bots WHERE uploaded_by = ? ORDER BY id DESC",
            (user_id,)
        ).fetchall()
        conn.close()

        if not user_bots:
            await update.message.reply_text(
                "⚠️ <b>No bots found!</b>\n\n"
                "You don't have any hosted bots. Upload a bot first, then send your <code>.env</code> file.",
                parse_mode="HTML"
            )
            return

        if len(user_bots) == 1:
            # Only one bot — save directly
            bot_row = dict(user_bots[0])
            USER_STATES[user_id] = {
                "state": "uploading_env_for_bot",
                "bot_id": bot_row["id"],
                "bot_folder": bot_row["folder"]
            }
        else:
            # Multiple bots — ask which one
            buttons = []
            for b in user_bots:
                buttons.append([
                    InlineKeyboardButton(
                        f"🤖 {b['name']}", callback_data=f"env_upload_for_{b['id']}"
                    )
                ])
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="close_menu")])
            USER_STATES[user_id] = {"state": "waiting_env_bot_select", "pending_doc": doc}
            await update.message.reply_text(
                "📄 <b>.env File Detected!</b>\n\n"
                "Which bot should this <code>.env</code> file be applied to?",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="HTML"
            )
            return

        # Download and save .env
        state = USER_STATES.pop(user_id, {})
        bot_folder = state["bot_folder"]
        env_dest = os.path.join(bot_folder, ".env")
        msg = await update.message.reply_text(
            "📥 <b>Saving .env file to your bot folder...</b>",
            parse_mode="HTML"
        )
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(env_dest)
            await msg.edit_text(
                "✅ <b>.env File Saved!</b>\n\n"
                f"Your <code>.env</code> config has been saved to bot <b>{dict(user_bots[0])['name']}</b>.\n\n"
                "💡 <i>Restart your bot from 📂 My Bots for the new config to take effect.</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            await msg.edit_text(f"❌ <b>Failed to save .env:</b> {e}", parse_mode="HTML")
        return


    # Show immediate feedback
    msg = await update.message.reply_text(
        f"📥 <b>System: Received <code>{filename}</code>. Initializing scan...</b>",
        parse_mode="HTML"
    )
    
    if USER_STATES[user_id].get("state") in ("waiting_for_fm_file_upload", "waiting_for_fm_zip_upload"):
        is_zip_upload = (USER_STATES[user_id].get("state") == "waiting_for_fm_zip_upload")
        bot_id = USER_STATES[user_id]["fm_bot_id"]
        rel_path = USER_STATES[user_id]["fm_path"]
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        dest_dir = os.path.join(bot["folder"], rel_path)
        
        if is_zip_upload:
            if not filename.endswith(".zip"):
                await msg.edit_text(
                    "❌ <b>Invalid File Type!</b> Please upload a valid <code>.zip</code> archive.",
                    parse_mode="HTML"
                )
                return
            await msg.edit_text(
                "📥 <b>Downloading ZIP archive to server...</b>",
                parse_mode="HTML"
            )
            dest_path = os.path.join(dest_dir, f"temp_upload_{int(time.time())}.zip")
            try:
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(dest_path)
                await msg.edit_text("⚙️ <b>Extracting ZIP archive...</b>", parse_mode="HTML")
                
                def unzip_file(zip_path, extract_path):
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(extract_path)
                    os.remove(zip_path)

                await asyncio.to_thread(unzip_file, dest_path, dest_dir)
                
                # Automatically scan and install dependencies if they changed/added new packages
                try:
                    await install_bot_dependencies(bot["folder"], bot["name"])
                except Exception:
                    pass
                
                await msg.edit_text(
                    f"✅ Archive extracted successfully in <code>{rel_path or '/'}</code>!",
                    parse_mode="HTML"
                )
            except Exception as e:
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                await msg.edit_text(
                    f"❌ <b>Extraction failed:</b> {e}",
                    parse_mode="HTML"
                )
        else:
            dest_path = os.path.join(dest_dir, filename)
            await msg.edit_text(
                "📥 <b>Uploading file to server...</b>",
                parse_mode="HTML"
            )
            try:
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(dest_path)
                await msg.edit_text(
                    f"✅ File <code>{filename}</code> uploaded successfully to <code>{rel_path or '/'}</code>!",
                    parse_mode="HTML"
                )
            except Exception as e:
                await msg.edit_text(
                    f"❌ <b>Upload failed:</b> {e}",
                    parse_mode="HTML"
                )
            
        USER_STATES[user_id]["state"] = None
        fake_msg = await update.message.reply_text(
            "Refreshing directory listing...",
            parse_mode="HTML"
        )
        await refresh_fm_interface(fake_msg, bot_id, user_id)
        return

    # Enforce slot limits for regular users
    if user_id != OWNER_ID:
        conn = get_db_connection()
        allowed_row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (user_id,)).fetchone()
        slots_limit = allowed_row["max_bots"] if (allowed_row and allowed_row["max_bots"] is not None) else 1
        bot_count = conn.execute("SELECT COUNT(*) FROM bots WHERE uploaded_by = ?", (user_id,)).fetchone()[0]
        conn.close()
        if bot_count >= slots_limit:
            await msg.edit_text(
                f"❌ <b>Slot Limit Reached!</b>\n\n"
                f"You have used <b>{bot_count}/{slots_limit}</b> bot slots.\n"
                "If you want to upload a new bot, please delete an existing bot or ask the Admin to increase your slot limit.",
                parse_mode="HTML"
            )
            USER_STATES.pop(user_id, None)
            return

    if not (filename.endswith(".py") or filename.endswith(".zip")):
        await msg.edit_text(
            "❌ <b>Invalid File Type!</b>\n\n"
            "Only <code>.py</code> files or <code>.zip</code> archives are supported. Please upload a valid code file:",
            parse_mode="HTML"
        )
        return
        
    await msg.edit_text(
        "📥 <b>[1/5] Downloading code files from Telegram...</b>",
        parse_mode="HTML"
    )
    
    temp_dir = os.path.join("bots", f"temp_{user_id}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    file_path = os.path.join(temp_dir, filename)
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(file_path)
    except Exception as de:
        shutil.rmtree(temp_dir)
        await msg.edit_text(
            f"❌ <b>Download failed:</b> {de}",
            parse_mode="HTML"
        )
        return
        
    await asyncio.sleep(0.8)
    await msg.edit_text(
        "⚙️ <b>[2/5] Creating secure workspace environment...</b>",
        parse_mode="HTML"
    )
    
    await asyncio.sleep(0.8)
    if filename.endswith(".zip"):
        await msg.edit_text(
            "📦 <b>[3/5] Extracting application archives...</b>",
            parse_mode="HTML"
        )
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            os.remove(file_path)
        except Exception as e:
            shutil.rmtree(temp_dir)
            await msg.edit_text(
                f"❌ <b>Extraction failed:</b> {e}",
                parse_mode="HTML"
            )
            return
    else:
        await msg.edit_text(
            "📄 <b>[3/5] Storing python script...</b>",
            parse_mode="HTML"
        )
        await asyncio.sleep(0.5)
        
    files = []
    for r, d, f_names in os.walk(temp_dir):
        for f in f_names:
            if f.endswith(".py"):
                files.append(f)
                
    if not files:
        shutil.rmtree(temp_dir)
        await msg.edit_text(
            "❌ <b>Verification Error:</b> No Python files (<code>.py</code>) found in the upload.",
            parse_mode="HTML"
        )
        USER_STATES.pop(user_id, None)
        return
        
    await asyncio.sleep(0.8)
    await msg.edit_text(
        "🔍 <b>[4/5] Scanning codebase for credentials and tokens...</b>",
        parse_mode="HTML"
    )
    
    token = find_token_in_directory(temp_dir)
    startup_file = find_startup_file(temp_dir)
    
    await asyncio.sleep(0.8)
    
    if token:
        await msg.edit_text(
            "🛰️ <b>[5/5] Connecting to Telegram API to verify bot credentials...</b>",
            parse_mode="HTML"
        )
        success, bot_name, bot_username = await verify_token_and_get_details(token)
        await asyncio.sleep(0.8)
        if success:
            USER_STATES[user_id] = {
                "state": "confirm_auto_registration",
                "temp_dir": temp_dir,
                "name": bot_name,
                "token": token,
                "startup_file": startup_file,
                "uploaded_filename": filename
            }
            masked_token = token[:9] + "..." + token[-4:]
            confirm_text = (
                "✨ <b>AUTOMATIC BOT DETECTION SUCCESS</b> ✨\n\n"
                f"🤖 <b>Bot Name</b>: <code>{bot_name}</code>\n"
                f"👤 <b>Username</b>: @{bot_username}\n"
                f"🔑 <b>Token</b>: <code>{masked_token}</code>\n"
                f"📄 <b>Startup File</b>: <code>{startup_file}</code>\n\n"
                "We found these details in your uploaded code! Would you like to register this bot?"
            )
            buttons = [
                [
                    InlineKeyboardButton("✅ Confirm & Register", callback_data="reg_confirm"),
                    InlineKeyboardButton("❌ Cancel", callback_data="reg_cancel")
                ],
                [
                    InlineKeyboardButton("📝 Edit Name", callback_data="reg_edit_name"),
                    InlineKeyboardButton("📄 Edit Startup File", callback_data="reg_edit_startup")
                ],
                [
                    InlineKeyboardButton("🔑 Edit Token", callback_data="reg_edit_token")
                ]
            ]
            await msg.edit_text(confirm_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
            return
        else:
            masked_token = token[:9] + "..." + token[-4:]
            USER_STATES[user_id] = {
                "state": "waiting_for_token",
                "temp_dir": temp_dir,
                "name": filename.replace(".py", "").replace(".zip", ""),
                "uploaded_filename": filename
            }
            await msg.edit_text(
                f"⚠️ <b>Token Verification Failed!</b>\n\n"
                f"We detected a token: <code>{masked_token}</code> but it failed to verify (Error: <code>{bot_name}</code>).\n\n"
                "Please enter a valid Telegram Bot Token manually:",
                parse_mode="HTML"
            )
            return
    else:
        USER_STATES[user_id] = {
            "state": "waiting_for_name",
            "temp_dir": temp_dir,
            "uploaded_filename": filename
        }
        await msg.edit_text(
            "📂 **File Uploaded Successfully!**\n\n"
            "⚠️ **No Telegram Bot Token detected in files.**\n\n"
            "🤖 **Step 1: Enter Bot Name**\n"
            "Send the display name for this bot (e.g. `My Awesome Bot`):"
        )

async def refresh_fm_interface(message, bot_id, user_id):
    conn = get_db_connection()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    rel_path = USER_STATES.get(user_id, {}).get("fm_path", "")
    full_dir = os.path.join(bot["folder"], rel_path)
    if not os.path.exists(full_dir):
        full_dir = bot["folder"]
        rel_path = ""
        USER_STATES[user_id]["fm_path"] = ""
        
    items = os.listdir(full_dir)
    buttons = []
    
    if rel_path != "":
        buttons.append([InlineKeyboardButton("🔙 [Parent Directory]", callback_data=f"fm_up_{bot_id}")])
        
    dirs = []
    files = []
    for item in items:
        item_path = os.path.join(full_dir, item)
        if os.path.isdir(item_path):
            dirs.append(item)
        else:
            files.append(item)
            
    dirs = sorted(dirs)
    files = sorted(files)
    
    if user_id not in USER_STATES:
        USER_STATES[user_id] = {}
    USER_STATES[user_id]["fm_dirs"] = dirs
    USER_STATES[user_id]["fm_files"] = files
            
    for idx, d in enumerate(dirs):
        buttons.append([InlineKeyboardButton(f"📁 {d}/", callback_data=f"fm_cd_{idx}")])
    for idx, f in enumerate(files):
        buttons.append([InlineKeyboardButton(f"📄 {f}", callback_data=f"fm_file_{idx}")])
        
    buttons.append([
        InlineKeyboardButton("📁 New Folder", callback_data=f"fm_mkdir_{bot_id}"),
        InlineKeyboardButton("📤 Upload File", callback_data=f"fm_upfile_{bot_id}"),
        InlineKeyboardButton("📦 Upload Archive", callback_data=f"fm_upzip_{bot_id}")
    ])
    buttons.append([
        InlineKeyboardButton("📦 Backup ZIP", callback_data=f"fm_zip_{bot_id}"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data=f"manage_{bot_id}")
    ])
    
    path_display = f"root/{rel_path}" if rel_path else "root"
    try:
        await message.edit_text(
            f"📁 <b>FILE MANAGER - {bot['name']}</b>\n\n"
            f"📍 Current Path: <code>{path_display}</code>\n\n"
            "Select folders to navigate or files to view options:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to edit file manager message: {e}")

async def show_file_actions_screen(query, bot_id, user_id, filename):
    rel_file_path = USER_STATES[user_id].get("fm_target_file", "")
    conn = get_db_connection()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    full_path = os.path.join(bot["folder"], rel_file_path)
    file_size = "Unknown"
    if os.path.exists(full_path):
        file_size = format_size(os.path.getsize(full_path))
        
    file_actions_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 View Content", callback_data=f"fma_view_{bot_id}"),
            InlineKeyboardButton("✍️ Edit Content", callback_data=f"fma_edit_{bot_id}")
        ],
        [
            InlineKeyboardButton("📥 Download", callback_data=f"fma_dl_{bot_id}"),
            InlineKeyboardButton("📝 Rename", callback_data=f"fma_rename_{bot_id}")
        ],
        [
            InlineKeyboardButton("🗑 Delete File", callback_data=f"fma_delete_{bot_id}"),
            InlineKeyboardButton("🔙 Back to Files", callback_data=f"fm_{bot_id}_refresh")
        ]
    ])
    
    await query.message.edit_text(
        f"📄 <b>FILE OPTIONS</b>\n\n"
        f"File: <code>{filename}</code>\n"
        f"Path: <code>{rel_file_path}</code>\n"
        f"Size: <code>{file_size}</code>\n\n"
        f"Select an action to perform on this file:",
        reply_markup=file_actions_keyboard,
        parse_mode="HTML"
    )
    await query.answer()

async def show_bot_dashboard(query, bot_id, user_id, context):
    conn = get_db_connection()
    bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
    conn.close()
    
    if not bot:
        await query.answer("Bot not found.", show_alert=True)
        return
        
    pid = bot["pid"]
    is_running = is_process_running(pid)
    
    current_status = "Running" if is_running else "Stopped"
    if current_status != bot["status"]:
        conn = get_db_connection()
        conn.execute("UPDATE bots SET status = ?, pid = ? WHERE id = ?", (current_status, pid if is_running else None, bot_id))
        conn.commit()
        conn.close()
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
    folder_size = format_size(get_folder_size(bot["folder"]))
    running_time = get_running_time(bot["last_started"]) if is_running else "N/A"
    status_icon = "🟢 Running" if is_running else "🔴 Stopped"
    
    bot_details = (
        "🤖 <b>BOT CONTROL DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Bot Name</b>: <code>{bot['name']}</code>\n"
        f"👤 <b>Uploader</b>: <code>{bot['uploaded_by'] or 'Admin'}</code>\n"
        f"📊 <b>Status</b>: {status_icon}\n"
        f"🆔 <b>PID</b>: <code>{bot['pid'] or 'None'}</code>\n"
        f"⏱ <b>Running Time</b>: <code>{running_time}</code>\n"
        f"📦 <b>Folder Size</b>: <code>{folder_size}</code>\n"
        f"🔄 <b>Auto-Restarts</b>: <code>{bot['restart_count']}/5</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 <b>Directory</b>: <code>{bot['folder']}</code>\n"
        f"📄 <b>Startup File</b>: <code>{bot['startup_file']}</code>\n\n"
        "💡 <i>Use the buttons below to Start, Stop, View Logs, Manage Files, or Delete this bot.</i>"
    )
    
    try:
        await query.message.edit_text(
            bot_details,
            reply_markup=get_bot_control_keyboard(bot_id, bool(bot["status_broadcast"]), is_admin=(user_id == OWNER_ID)),
            parse_mode="HTML"
        )
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Failed to edit bot dashboard message: {e}")
    try:
        await query.answer()
    except Exception:
        pass

class SafeCallbackQuery:
    def __init__(self, query):
        object.__setattr__(self, "_query", query)
        
    def __getattr__(self, name):
        return getattr(self._query, name)
        
    def __setattr__(self, name, value):
        try:
            setattr(self._query, name, value)
        except AttributeError:
            object.__setattr__(self, name, value)
            
    async def answer(self, *args, **kwargs):
        try:
            return await self._query.answer(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Could not answer callback query safely: {e}")

@owner_only_check
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = SafeCallbackQuery(update.callback_query)
    user_id = query.from_user.id
    data = query.data
    
    # Ownership authorization check
    bot_id = None
    if "_" in data:
        parts = data.split("_")
        if parts[0] in ("manage", "start", "stop", "restart", "logs") and len(parts) > 1:
            try:
                bot_id = int(parts[1])
            except ValueError:
                pass
        elif parts[0] == "fm" and len(parts) > 1:
            try:
                if parts[1] in ("cd", "pending"):
                    if user_id in USER_STATES:
                        bot_id = USER_STATES[user_id].get("fm_bot_id")
                elif parts[1] in ("up", "upfile", "zip", "mkdir", "upzip") and len(parts) > 2:
                    bot_id = int(parts[2])
                elif parts[1].isdigit():
                    bot_id = int(parts[1])
            except ValueError:
                pass
        elif parts[0] == "delete" and len(parts) > 2:
            try:
                bot_id = int(parts[2])
            except ValueError:
                pass
        elif parts[0] in ("dl", "clear") and len(parts) > 2:
            try:
                bot_id = int(parts[2])
            except ValueError:
                pass
        elif parts[0] == "fma" and len(parts) > 2:
            try:
                bot_id = int(parts[2])
            except ValueError:
                pass
        elif parts[0] == "toggle" and len(parts) > 2:
            try:
                bot_id = int(parts[2])
            except ValueError:
                pass

    if bot_id is not None and user_id != OWNER_ID:
        conn = get_db_connection()
        bot = conn.execute("SELECT uploaded_by FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        if bot and bot["uploaded_by"] != user_id:
            await query.answer("⛔ Access Denied: You do not own this bot.", show_alert=True)
            return

    if data == "close_menu":
        await query.message.delete()
        await query.answer()
        return
        
    elif data == "speed_refresh":
        await send_speed_stats(query.message, user_id, is_callback=True)
        await query.answer("Hardware stats refreshed!")
        return
        
    elif data == "speedtest_run":
        await query.answer("Running network speed test... Please wait.")
        try:
            await query.message.edit_text(
                "⚡ <b>SYSTEM PERFORMANCE MONITOR</b> ⚡\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📡 <b>Running Internet speed test...</b>\n"
                "📥 <i>Downloading test buffers (Cloudflare)...</i>\n"
                "📤 <i>Uploading verification packets...</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏳ <i>This may take up to 5-10 seconds.</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
            
        success, dl_speed, ul_speed = await perform_network_speedtest()
        
        # Recalculate original stats to display along with network speeds
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('.')
        
        conn = get_db_connection()
        total_bots = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        conn.close()
        
        running_bots = 0
        conn = get_db_connection()
        bots = conn.execute("SELECT pid FROM bots").fetchall()
        conn.close()
        for b in bots:
            if is_process_running(b["pid"]):
                running_bots += 1
                
        def make_progress_bar(percent):
            filled = int(round(percent / 10))
            bar = "█" * filled + "░" * (10 - filled)
            return f"<code>[{bar}] {percent:.1f}%</code>"
            
        if success:
            net_status = (
                f"📥 <b>Download Speed</b>: <code>{dl_speed:.2f} Mbps</code>\n"
                f"📤 <b>Upload Speed</b>: <code>{ul_speed:.2f} Mbps</code>\n"
                f"🚀 <b>Peak Capacity</b>: <code>{max(dl_speed, ul_speed) * 1.25:.2f} Mbps</code>\n"
            )
        else:
            net_status = "❌ <b>Network Speedtest</b>: <code>Failed to contact speed server</code>\n"
            
        performance_text = (
            "⚡ <b>SYSTEM PERFORMANCE MONITOR</b> ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💻 <b>CPU Load</b>: {make_progress_bar(cpu_percent)}\n"
            f"🧠 <b>RAM Usage</b>: {make_progress_bar(memory.percent)} ({format_size(memory.used)} / {format_size(memory.total)})\n"
            f"💽 <b>Disk Space</b>: {make_progress_bar(disk.percent)} ({format_size(disk.used)} / {format_size(disk.total)})\n"
            f"🤖 <b>Active Bots</b>: <code>{running_bots} / {total_bots} Running</code>\n\n"
            "📡 <b>NETWORK SPEED METRICS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{net_status}"
            f"🕒 <b>Last Updated</b>: <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 <i>Real-time hardware status metrics.</i>"
        )
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh Hardware", callback_data="speed_refresh"),
                InlineKeyboardButton("⚡ Run Net Speedtest", callback_data="speedtest_run")
            ],
            [
                InlineKeyboardButton("❌ Close Stats", callback_data="close_menu")
            ]
        ])
        
        try:
            await query.message.edit_text(performance_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Failed to edit speed stats: {e}")
        return
        
    elif data.startswith("toggle_bc_"):
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
        bot_id = int(data.split("_")[2])
        conn = get_db_connection()
        bot = conn.execute("SELECT status_broadcast FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if bot:
            new_val = 1 if bot["status_broadcast"] == 0 else 0
            conn.execute("UPDATE bots SET status_broadcast = ? WHERE id = ?", (new_val, bot_id))
            conn.commit()
            status_str = "enabled" if new_val == 1 else "disabled"
            await query.answer(f"Status broadcast {status_str}!", show_alert=False)
        conn.close()
        
        await show_bot_dashboard(query, bot_id, user_id, context)
        return

    # ── ✏️ RENAME BOT ────────────────────────────────────────────────
    elif data.startswith("rename_bot_"):
        bot_id = int(data.split("_")[2])
        conn = get_db_connection()
        bot = conn.execute("SELECT name FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        if not bot:
            await query.answer("Bot not found.", show_alert=True)
            return
        USER_STATES[user_id] = {"state": "waiting_for_bot_rename", "bot_id": bot_id}
        await query.message.edit_text(
            f"✏️ <b>RENAME BOT</b>\n\n"
            f"Current name: <code>{bot['name']}</code>\n\n"
            "Please send the <b>new name</b> for this bot in your next message:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data=f"manage_{bot_id}")
            ]])
        )
        await query.answer()
        return

    # ── 📄 .env upload for a specific bot (multi-bot selector) ──────
    elif data.startswith("env_upload_for_"):
        bot_id = int(data.split("_")[-1])
        conn = get_db_connection()
        bot = conn.execute("SELECT name, folder FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        if not bot:
            await query.answer("Bot not found.", show_alert=True)
            return

        # Get the pending .env document
        state = USER_STATES.get(user_id, {})
        doc = state.get("pending_doc")
        if not doc:
            await query.answer("Session expired. Please resend the .env file.", show_alert=True)
            return

        env_dest = os.path.join(bot["folder"], ".env")
        await query.message.edit_text(
            f"📥 <b>Saving .env to bot {bot['name']}...</b>",
            parse_mode="HTML"
        )
        try:
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(env_dest)
            USER_STATES.pop(user_id, None)
            await query.message.edit_text(
                f"✅ <b>.env Saved to {bot['name']}!</b>\n\n"
                "💡 <i>Restart the bot from 📂 My Bots for the new config to take effect.</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.message.edit_text(f"❌ <b>Failed to save .env:</b> {e}", parse_mode="HTML")
        await query.answer()
        return


    elif data == "admin_all_bots":
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only.", show_alert=True)
            return
        conn = get_db_connection()
        all_bots = conn.execute(
            "SELECT b.id, b.name, b.status, b.pid, b.uploaded_by FROM bots b ORDER BY b.uploaded_by, b.id"
        ).fetchall()
        conn.close()

        if not all_bots:
            await query.answer("No bots registered yet.", show_alert=True)
            return

        text = "📋 <b>ALL HOSTED BOTS — ADMIN VIEW</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        buttons = []
        for b in all_bots:
            is_running = is_process_running(b["pid"])
            icon = "🟢" if is_running else "🔴"
            owner_label = f"(User: {b['uploaded_by'] or 'Admin'})"
            text += f"{icon} <b>{b['name']}</b> {owner_label}\n"
            action = "stop" if is_running else "start"
            action_label = "⏹ Stop" if is_running else "▶ Start"
            buttons.append([
                InlineKeyboardButton(f"{action_label} {b['name'][:18]}", callback_data=f"{action}_{b['id']}"),
                InlineKeyboardButton("🔧 Manage", callback_data=f"manage_{b['id']}")
            ])

        text += f"\n<i>Total: {len(all_bots)} bots registered.</i>"
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="user_manager_back")])

        try:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"admin_all_bots error: {e}")
        await query.answer()
        return

    elif data == "user_add_prompt":

        USER_STATES[user_id] = {"state": "waiting_for_add_userid"}
        await query.message.edit_text(
            "➕ <b>ADD AUTHORIZED USER</b>\n\n"
            "Please send the numerical Telegram User ID of the user you want to authorize in your next message (e.g. <code>123456789</code>):\n\n"
            "<i>Note: You can find user IDs using bots like @username_to_id_bot or @MissRose_bot.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Users", callback_data="user_manager_back")]])
        )
        await query.answer()
        return
        
    elif data.startswith("user_revoke_"):
        target_user_id = int(data.split("_")[2])
        conn = get_db_connection()
        conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (target_user_id,))
        conn.commit()
        conn.close()
        await query.answer(f"Revoked permission for user ID {target_user_id}", show_alert=True)
        
        class FakeUpdate:
            def __init__(self, q):
                self.callback_query = q
                self.message = None
        await show_users_manager(FakeUpdate(query), context)
        return
        
    elif data == "user_manager_back":
        USER_STATES.pop(user_id, None)
        class FakeUpdate:
            def __init__(self, q):
                self.callback_query = q
                self.message = None
        await show_users_manager(FakeUpdate(query), context)
        return
        
    elif data.startswith("user_manage_"):
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
        target_user_id = int(data.split("_")[2])
        await show_single_user_manager(query, target_user_id, context)
        return

    elif data.startswith("user_addslot_"):
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
        target_user_id = int(data.split("_")[2])
        conn = get_db_connection()
        row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (target_user_id,)).fetchone()
        if not row:
            conn.close()
            await query.answer("User access not found or revoked.", show_alert=True)
            return
        current_limit = row["max_bots"] if row["max_bots"] is not None else 1
        new_limit = current_limit + 1
        conn.execute("UPDATE allowed_users SET max_bots = ? WHERE user_id = ?", (new_limit, target_user_id))
        conn.commit()
        conn.close()
        
        # Notify target user
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"ℹ️ <b>Slots Limit Increased!</b>\n\nYour bot slots limit has been increased to <b>{new_limit}</b> by the Admin.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not notify user {target_user_id} of slot limit increase: {e}")
            
        await query.answer(f"Slot limit increased to {new_limit}!", show_alert=False)
        await show_single_user_manager(query, target_user_id, context)
        return

    elif data.startswith("user_removeslot_"):
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
        target_user_id = int(data.split("_")[2])
        conn = get_db_connection()
        row = conn.execute("SELECT max_bots FROM allowed_users WHERE user_id = ?", (target_user_id,)).fetchone()
        if not row:
            conn.close()
            await query.answer("User access not found or revoked.", show_alert=True)
            return
        current_limit = row["max_bots"] if row["max_bots"] is not None else 1
        if current_limit <= 1:
            conn.close()
            await query.answer("Slot limit cannot be less than 1.", show_alert=True)
            return
        new_limit = current_limit - 1
        conn.execute("UPDATE allowed_users SET max_bots = ? WHERE user_id = ?", (new_limit, target_user_id))
        conn.commit()
        conn.close()
        
        # Notify target user
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"ℹ️ <b>Slots Limit Decreased!</b>\n\nYour bot slots limit has been decreased to <b>{new_limit}</b> by the Admin.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not notify user {target_user_id} of slot limit decrease: {e}")
            
        await query.answer(f"Slot limit decreased to {new_limit}!", show_alert=False)
        await show_single_user_manager(query, target_user_id, context)
        return

    elif data.startswith("sys_restore_confirm_"):
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
            
        state_info = USER_STATES.get(user_id)
        if not state_info or state_info.get("state") != "confirm_system_restore":
            await query.answer("Session expired or invalid restore state.", show_alert=True)
            return
            
        backup_path = state_info.get("backup_path")
        if not backup_path or not os.path.exists(backup_path):
            await query.answer("Backup ZIP file not found on server.", show_alert=True)
            return
            
        await query.answer("Restoring system state... Please wait.", show_alert=False)
        edit_msg = await query.message.edit_text(
            "⏳ <b>SYSTEM RESTORE IN PROGRESS</b> ⏳\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🛑 <i>Step 1: Stopping all active uploader bots...</i>",
            parse_mode="HTML"
        )
        
        # Stop all running bots
        try:
            conn = get_db_connection()
            bots = conn.execute("SELECT id FROM bots").fetchall()
            conn.close()
            for b in bots:
                try:
                    stop_bot_process(b["id"])
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error stopping bots during restore: {e}")
            
        await asyncio.sleep(1.0)
        await edit_msg.edit_text(
            "⏳ <b>SYSTEM RESTORE IN PROGRESS</b> ⏳\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚙️ <i>Step 2: Extracting files and database...</i>",
            parse_mode="HTML"
        )
        
        # Extract files safely
        try:
            with zipfile.ZipFile(backup_path, 'r') as zip_ref:
                for member in zip_ref.namelist():
                    if ".." in member or member.startswith("/"):
                        continue
                    
                    target_file_path = os.path.normpath(member)
                    
                    parent_dir = os.path.dirname(target_file_path)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)
                        
                    if os.path.isdir(target_file_path) or member.endswith("/"):
                        continue
                        
                    try:
                        zip_ref.extract(member, ".")
                    except PermissionError:
                        if target_file_path == "hosting.py":
                            old_path = "hosting.py.old"
                            if os.path.exists(old_path):
                                try:
                                    os.remove(old_path)
                                except Exception:
                                    pass
                            try:
                                os.rename("hosting.py", old_path)
                                zip_ref.extract(member, ".")
                            except Exception as re:
                                logger.error(f"Failed to rename/overwrite hosting.py: {re}")
                                raise
                        else:
                            raise
        except Exception as ex:
            logger.error(f"Failed during ZIP extraction: {ex}")
            await edit_msg.edit_text(f"❌ <b>Extraction failed:</b> <code>{ex}</code>", parse_mode="HTML")
            try:
                os.remove(backup_path)
            except Exception:
                pass
            USER_STATES.pop(user_id, None)
            return
            
        os.makedirs("logs", exist_ok=True)
        
        try:
            os.remove(backup_path)
        except Exception:
            pass
            
        USER_STATES.pop(user_id, None)
        
        await edit_msg.edit_text(
            "✅ <b>SYSTEM RESTORE COMPLETED!</b> ✅\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Successfully restored database, settings, and bots directory.\n\n"
            "🔄 <b>Rebooting Hosting Manager now...</b>",
            parse_mode="HTML"
        )
        
        await asyncio.sleep(1.5)
        restart_hosting_bot()
        return

    elif data == "sys_restore_cancel":
        if user_id != OWNER_ID:
            await query.answer("⛔ Access Denied: Admin only feature.", show_alert=True)
            return
            
        state_info = USER_STATES.pop(user_id, None)
        if state_info and "backup_path" in state_info:
            bp = state_info["backup_path"]
            if os.path.exists(bp):
                try:
                    os.remove(bp)
                except Exception:
                    pass
                    
        await query.message.edit_text("❌ <b>System restore cancelled.</b> Temp backup files deleted.", parse_mode="HTML")
        await query.answer()
        return

    elif data == "fm_pending_upload":
        bot_id = USER_STATES[user_id].get("fm_bot_id")
        rel_path = USER_STATES[user_id].get("fm_path", "")
        doc = USER_STATES[user_id].get("pending_doc")
        
        if not bot_id or not doc:
            await query.answer("Session expired or invalid file state.", show_alert=True)
            return
            
        filename = doc.file_name
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        dest_dir = os.path.join(bot["folder"], rel_path)
        
        await query.message.edit_text(
            f"📥 <b>Processing pending file:</b> <code>{filename}</code>...",
            parse_mode="HTML"
        )
        
        if filename.endswith(".zip"):
            dest_path = os.path.join(dest_dir, f"temp_upload_{int(time.time())}.zip")
            try:
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(dest_path)
                await query.message.edit_text("⚙️ <b>Extracting ZIP archive...</b>", parse_mode="HTML")
                
                with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                    zip_ref.extractall(dest_dir)
                os.remove(dest_path)
                
                # Scan & install dependencies
                try:
                    await install_bot_dependencies(bot["folder"], bot["name"])
                except Exception:
                    pass
                
                await query.message.edit_text(
                    f"✅ Archive extracted successfully in <code>{rel_path or '/'}</code>!",
                    parse_mode="HTML"
                )
            except Exception as e:
                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                await query.message.edit_text(
                    f"❌ <b>Extraction failed:</b> {e}",
                    parse_mode="HTML"
                )
        else:
            dest_path = os.path.join(dest_dir, filename)
            try:
                tg_file = await doc.get_file()
                await tg_file.download_to_drive(dest_path)
                await query.message.edit_text(
                    f"✅ File <code>{filename}</code> uploaded successfully to <code>{rel_path or '/'}</code>!",
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.message.edit_text(
                    f"❌ <b>Upload failed:</b> {e}",
                    parse_mode="HTML"
                )
                
        USER_STATES[user_id]["state"] = None
        USER_STATES[user_id].pop("pending_doc", None)
        
        fake_msg = await query.message.reply_text("Refreshing directory listing...", parse_mode="HTML")
        await refresh_fm_interface(fake_msg, bot_id, user_id)
        await query.answer()
        return
        
    elif data == "fm_pending_register":
        doc = USER_STATES[user_id].get("pending_doc")
        if not doc:
            await query.answer("No pending file found.", show_alert=True)
            return
            
        # Clear fm details to avoid registration going to FM directory
        USER_STATES[user_id] = {
            "state": "waiting_for_file"
        }
        
        # Build fake update and message objects
        class FakeMessage:
            def __init__(self, message, document):
                self.message_id = message.message_id
                self.chat = message.chat
                self.date = message.date
                self.document = document
                self.reply_markup = message.reply_markup
                self._message = message
            async def reply_text(self, *args, **kwargs):
                return await self._message.reply_text(*args, **kwargs)
            async def reply_photo(self, *args, **kwargs):
                return await self._message.reply_photo(*args, **kwargs)
                
        class FakeUpdate:
            def __init__(self, query, document):
                self.callback_query = query
                self.effective_user = query.from_user
                self.message = FakeMessage(query.message, document)
                
        # Call file_upload_handler directly
        await file_upload_handler(FakeUpdate(query, doc), context)
        await query.answer()
        return
        
    elif data == "fm_pending_cancel":
        if user_id in USER_STATES:
            USER_STATES[user_id].pop("pending_doc", None)
        await query.message.delete()
        await query.answer("Upload cancelled.")
        return
        
    elif data == "reg_confirm":
        # Answer immediately to close the loading state in Telegram
        await query.answer()
        
        state_info = USER_STATES.get(user_id)
        if not state_info or state_info.get("state") != "confirm_auto_registration":
            await query.message.edit_text("❌ Session expired or invalid state.", reply_markup=None)
            return
            
        bot_name = state_info["name"]
        bot_token = state_info["token"]
        startup_file = state_info["startup_file"]
        temp_dir = state_info["temp_dir"]
        
        await query.message.edit_text("⚡ Registering bot on server, please wait...")
        success, err, final_folder = await register_new_bot(bot_name, bot_token, temp_dir, startup_file, user_id, update=query)
        
        if success:
            USER_STATES.pop(user_id, None)
            success_text = (
                "🎉 <b>Bot Registered Successfully!</b>\n\n"
                f"🤖 <b>Bot Name</b>: <code>{bot_name}</code>\n"
                f"📂 <b>Folder</b>: <code>{final_folder}</code>\n"
                f"📄 <b>Startup File</b>: <code>{startup_file}</code>\n\n"
                "You can now manage it from the <b>📂 My Bots</b> menu."
            )
            await query.message.edit_text(success_text, reply_markup=None, parse_mode="HTML")
            await query.message.reply_text("📋 Main menu loaded.", reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
        else:
            await query.message.edit_text(f"❌ Registration failed: {err}")
        return
        
    elif data == "reg_cancel":
        state_info = USER_STATES.pop(user_id, None)
        if state_info and "temp_dir" in state_info and os.path.exists(state_info["temp_dir"]):
            try:
                shutil.rmtree(state_info["temp_dir"])
            except Exception:
                pass
        await query.message.edit_text(
            "❌ <b>Upload and registration process cancelled.</b>",
            reply_markup=None,
            parse_mode="HTML"
        )
        await query.message.reply_text(
            "📋 <b>Main menu loaded.</b>",
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )
        await query.answer()
        return
        
    elif data == "reg_edit_name":
        state_info = USER_STATES.get(user_id)
        if state_info:
            state_info["state"] = "waiting_for_name_edit"
            await query.message.edit_text(
                "✏️ <b>Edit Bot Name</b>\n\nPlease enter the new name for this bot in your next message:",
                parse_mode="HTML"
            )
        await query.answer()
        return
        
    elif data == "reg_edit_startup":
        state_info = USER_STATES.get(user_id)
        if state_info:
            state_info["state"] = "waiting_for_startup_edit"
            await query.message.edit_text(
                "✏️ <b>Edit Startup File</b>\n\nPlease enter the Python startup file name (e.g. <code>main.py</code>):",
                parse_mode="HTML"
            )
        await query.answer()
        return
        
    elif data == "reg_edit_token":
        state_info = USER_STATES.get(user_id)
        if state_info:
            state_info["state"] = "waiting_for_token_edit"
            await query.message.edit_text(
                "✏️ <b>Edit Bot Token</b>\n\nPlease send the new Bot Token from @BotFather in your next message:",
                parse_mode="HTML"
            )
        await query.answer()
        return
        
    elif data == "back_to_list":
        await query.message.edit_text(
            "📂 <b>My Hosted Bots</b>\n\nSelect a bot from the list below to view status and manage controls:",
            reply_markup=get_bots_keyboard(user_id),
            parse_mode="HTML"
        )
        await query.answer()
        return
        
    elif data.startswith("manage_"):
        bot_id = int(data.split("_")[1])
        await show_bot_dashboard(query, bot_id, user_id, context)
        return
        
    elif data.startswith("start_"):
        bot_id = int(data.split("_")[1])
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        if is_process_running(bot["pid"]):
            await query.answer("Bot is already running!", show_alert=True)
            return
            
        await query.message.edit_text("⚡ Starting bot service, please wait...")
        success, detail = await start_bot_process(bot_id, update=query)
        
        if success:
            await query.answer("Bot started successfully!", show_alert=False)
        else:
            await query.answer(f"Failed to start: {detail}", show_alert=True)
            
        await show_bot_dashboard(query, bot_id, user_id, context)
        return
        
    elif data.startswith("stop_"):
        bot_id = int(data.split("_")[1])
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        if not is_process_running(bot["pid"]):
            await query.answer("Bot is already stopped!", show_alert=True)
            return
            
        await query.message.edit_text("⏹ Stopping bot service...")
        stop_bot_process(bot_id)
        await query.answer("Bot stopped.", show_alert=False)
        
        await show_bot_dashboard(query, bot_id, user_id, context)
        return
        
    elif data.startswith("restart_"):
        bot_id = int(data.split("_")[1])
        await query.message.edit_text("🔄 Restarting bot service...")
        stop_bot_process(bot_id)
        time.sleep(2)
        success, detail = await start_bot_process(bot_id, update=query)
        
        if success:
            await query.answer("Bot restarted successfully!", show_alert=False)
        else:
            await query.answer(f"Restart failed: {detail}", show_alert=True)
            
        await show_bot_dashboard(query, bot_id, user_id, context)
        return
        
    elif data.startswith("delete_confirm_"):
        bot_id = int(data.split("_")[2])
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🛑 Yes, Delete", callback_data=f"delete_execute_{bot_id}"),
                InlineKeyboardButton("❌ No, Keep", callback_data=f"manage_{bot_id}")
            ]
        ])
        await query.message.edit_text(
            f"⚠️ <b>ARE YOU SURE?</b>\n\nThis will permanently delete <b>{bot['name']}</b>, stop the process, and delete all of its files from disk.\n\nThis action cannot be undone!",
            reply_markup=confirm_keyboard,
            parse_mode="HTML"
        )
        await query.answer()
        
    elif data.startswith("delete_execute_"):
        bot_id = int(data.split("_")[2])
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        if not bot:
            await query.answer("Bot not found.", show_alert=True)
            return
            
        stop_bot_process(bot_id)
        
        folder = bot["folder"]
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder)
            except Exception as e:
                logger.error(f"Error removing folder {folder}: {e}")
                
        log_path = os.path.join("logs", f"bot_{bot_id}.log")
        if os.path.exists(log_path):
            try:
                os.remove(log_path)
            except Exception:
                pass
                
        conn = get_db_connection()
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        conn.commit()
        conn.close()
        
        await query.answer("Bot deleted successfully.", show_alert=True)
        await query.message.edit_text(
            "📂 <b>My Hosted Bots</b>\n\nSelect a bot from the list below to view status and manage controls:",
            reply_markup=get_bots_keyboard(user_id),
            parse_mode="HTML"
        )
        
    elif data.startswith("logs_"):
        bot_id = int(data.split("_")[1])
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        log_path = os.path.join("logs", f"bot_{bot_id}.log")
        
        log_content = "📂 Log file is empty or does not exist yet."
        log_size = "0 B"
        if os.path.exists(log_path):
            log_size = format_size(os.path.getsize(log_path))
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                last_lines = lines[-50:]
                if last_lines:
                    log_content = "".join(last_lines)
                    if len(log_content) > 3800:
                        log_content = "...\n" + log_content[-3800:]
                        
        logs_menu_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh Logs", callback_data=f"logs_{bot_id}"),
                InlineKeyboardButton("📥 Download Log File", callback_data=f"dl_log_{bot_id}")
            ],
            [
                InlineKeyboardButton("🧹 Clear Logs", callback_data=f"clear_log_{bot_id}"),
                InlineKeyboardButton("🔙 Back to Bot", callback_data=f"manage_{bot_id}")
            ]
        ])
        
        import html
        escaped_log = html.escape(log_content)
        try:
            await query.message.edit_text(
                f"📜 <b>LOG VIEWER - {bot['name']}</b>\n"
                f"📊 Log Size: <code>{log_size}</code> | 🕒 Checked: <code>{datetime.now().strftime('%H:%M:%S')}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<pre>{escaped_log}</pre>",
                reply_markup=logs_menu_keyboard,
                parse_mode="HTML"
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Failed to edit log viewer message: {e}")
        await query.answer("Logs updated!")
        
    elif data.startswith("dl_log_"):
        bot_id = int(data.split("_")[2])
        log_path = os.path.join("logs", f"bot_{bot_id}.log")
        
        if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
            await query.answer("Log file is empty or doesn't exist.", show_alert=True)
            return
            
        await query.answer("Sending log file...")
        await context.bot.send_document(chat_id=user_id, document=open(log_path, 'rb'), filename=f"bot_{bot_id}_log.txt", caption=f"📜 Log backup for bot ID {bot_id}")
        
    elif data.startswith("clear_log_"):
        bot_id = int(data.split("_")[2])
        log_path = os.path.join("logs", f"bot_{bot_id}.log")
        
        if os.path.exists(log_path):
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("")
                
        await query.answer("Logs cleared successfully!")
        class FakeUpdate:
            def __init__(self, q):
                self.callback_query = q
        query.data = f"logs_{bot_id}"
        await callback_handler(FakeUpdate(query), context)
        
    elif data.startswith("fm_") and len(data.split("_")) > 1 and data.split("_")[1].isdigit():
        parts = data.split("_")
        bot_id = int(parts[1])
        
        if user_id not in USER_STATES:
            USER_STATES[user_id] = {}
        if len(parts) == 2:
            USER_STATES[user_id]["fm_path"] = ""
        USER_STATES[user_id]["fm_bot_id"] = bot_id
        
        await refresh_fm_interface(query.message, bot_id, user_id)
        await query.answer()
        
    elif data.startswith("fm_cd_"):
        idx = int(data[6:])
        bot_id = USER_STATES[user_id]["fm_bot_id"]
        dirs = USER_STATES[user_id].get("fm_dirs", [])
        if idx < len(dirs):
            folder_name = dirs[idx]
            current_rel = USER_STATES[user_id].get("fm_path", "")
            new_rel = os.path.join(current_rel, folder_name) if current_rel else folder_name
            USER_STATES[user_id]["fm_path"] = new_rel
            
            await refresh_fm_interface(query.message, bot_id, user_id)
            await query.answer()
        else:
            await query.answer("Folder list expired. Please refresh.", show_alert=True)
            
    elif data.startswith("fm_up_"):
        bot_id = int(data.split("_")[2])
        current_rel = USER_STATES[user_id].get("fm_path", "")
        parent_rel = os.path.dirname(current_rel)
        USER_STATES[user_id]["fm_path"] = parent_rel
        
        await refresh_fm_interface(query.message, bot_id, user_id)
        await query.answer()
        
    elif data.startswith("fm_file_"):
        idx = int(data[8:])
        bot_id = USER_STATES[user_id]["fm_bot_id"]
        rel_path = USER_STATES[user_id].get("fm_path", "")
        files = USER_STATES[user_id].get("fm_files", [])
        if idx < len(files):
            filename = files[idx]
            USER_STATES[user_id]["fm_target_file"] = os.path.join(rel_path, filename) if rel_path else filename
            await show_file_actions_screen(query, bot_id, user_id, filename)
        else:
            await query.answer("File list expired. Please refresh.", show_alert=True)
            
    elif data.startswith("fma_info_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id].get("fm_target_file", "")
        filename = os.path.basename(rel_file_path)
        await show_file_actions_screen(query, bot_id, user_id, filename)
        
    elif data.startswith("fma_view_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id]["fm_target_file"]
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        full_path = os.path.join(bot["folder"], rel_file_path)
        if not os.path.exists(full_path):
            await query.answer("File not found.", show_alert=True)
            return
            
        if os.path.getsize(full_path) > 50 * 1024:
            await query.answer("⚠️ File is too large to view directly (Max: 50 KB). Please use Download.", show_alert=True)
            return
            
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            await query.answer("⚠️ Binary files cannot be viewed as text.", show_alert=True)
            return
            
        import html
        escaped_content = html.escape(content)
        
        view_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✍️ Edit Content", callback_data=f"fma_edit_{bot_id}"),
                InlineKeyboardButton("🔙 Back to Options", callback_data=f"fma_info_{bot_id}")
            ]
        ])
        
        header = f"📄 <b>VIEWING FILE:</b> <code>{os.path.basename(rel_file_path)}</code>\n\n"
        footer = "\n"
        max_len = 4000 - len(header) - len(footer)
        
        if len(escaped_content) <= max_len:
            await query.message.edit_text(
                f"{header}<pre>{escaped_content}</pre>{footer}",
                reply_markup=view_keyboard,
                parse_mode="HTML"
            )
        else:
            truncated_content = escaped_content[:max_len] + "\n... [Truncated due to Telegram size limit] ..."
            await query.message.edit_text(
                f"{header}<pre>{truncated_content}</pre>{footer}",
                reply_markup=view_keyboard,
                parse_mode="HTML"
            )
        await query.answer()
        
    elif data.startswith("fma_dl_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id]["fm_target_file"]
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        full_path = os.path.join(bot["folder"], rel_file_path)
        if os.path.exists(full_path):
            await query.answer("Sending file...")
            await context.bot.send_document(chat_id=user_id, document=open(full_path, 'rb'), filename=os.path.basename(full_path))
        else:
            await query.answer("File not found.", show_alert=True)
            
    elif data.startswith("fma_delete_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id]["fm_target_file"]
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        full_path = os.path.join(bot["folder"], rel_file_path)
        if os.path.exists(full_path):
            try:
                os.remove(full_path)
                await query.answer("File deleted successfully!", show_alert=True)
            except Exception as e:
                await query.answer(f"Failed to delete: {e}", show_alert=True)
        else:
            await query.answer("File not found.", show_alert=True)
            
        await refresh_fm_interface(query.message, bot_id, user_id)
        
    elif data.startswith("fma_rename_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id]["fm_target_file"]
        
        USER_STATES[user_id]["state"] = "waiting_for_rename"
        await query.message.edit_text(
            f"📝 <b>RENAME FILE</b>\n\n"
            f"Current file path: <code>{rel_file_path}</code>\n\n"
            "Please send the new name for this file in your next message (e.g. <code>config_new.py</code>):",
            parse_mode="HTML"
        )
        await query.answer()
        
    elif data.startswith("fma_edit_"):
        bot_id = int(data.split("_")[2])
        rel_file_path = USER_STATES[user_id]["fm_target_file"]
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        full_path = os.path.join(bot["folder"], rel_file_path)
        if not os.path.exists(full_path):
            await query.answer("File not found.", show_alert=True)
            return
            
        if os.path.getsize(full_path) > 100 * 1024:
            await query.answer("⚠️ File is too large to edit directly in Telegram (Max: 100 KB).", show_alert=True)
            return
            
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            await query.answer("⚠️ Binary files cannot be edited as text.", show_alert=True)
            return
            
        USER_STATES[user_id]["state"] = "waiting_for_file_content_edit"
        
        content_preview = ""
        if len(content) <= 3000:
            import html
            content_preview = f"Current Content:\n<pre>{html.escape(content)}</pre>\n\n"
            
        await query.message.edit_text(
            f"✍️ <b>EDIT FILE CONTENT</b>\n\n"
            f"File: <code>{os.path.basename(rel_file_path)}</code>\n\n"
            f"{content_preview}"
            "Please send the new code/text content for this file in your next message. It will overwrite the existing file:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"fma_info_{bot_id}")]])
        )
        await query.answer()
        
    elif data.startswith("fm_mkdir_"):
        bot_id = int(data.split("_")[2])
        USER_STATES[user_id]["state"] = "waiting_for_mkdir"
        USER_STATES[user_id]["fm_bot_id"] = bot_id
        
        await query.message.edit_text(
            "📁 <b>Create New Folder</b>\n\n"
            "Please send the folder name you want to create in the current directory:\n\n"
            f"📍 Path: <code>root/{USER_STATES[user_id].get('fm_path', '')}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"fm_{bot_id}_refresh")]])
        )
        await query.answer()
        
    elif data.startswith("fm_upfile_"):
        bot_id = int(data.split("_")[2])
        USER_STATES[user_id]["state"] = "waiting_for_fm_file_upload"
        
        await query.message.edit_text(
            "📤 <b>Upload File to Directory</b>\n\n"
            "Please send any file (document) to upload it directly to the current directory.\n\n"
            f"📍 Destination Path: <code>root/{USER_STATES[user_id].get('fm_path', '')}</code>\n\n"
            "Waiting for file upload...",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"fm_{bot_id}_refresh")]])
        )
        await query.answer()
        
    elif data.startswith("fm_upzip_"):
        bot_id = int(data.split("_")[2])
        USER_STATES[user_id]["state"] = "waiting_for_fm_zip_upload"
        
        await query.message.edit_text(
            "📦 <b>Upload Archive (ZIP)</b>\n\n"
            "Please send a <code>.zip</code> file. It will be downloaded and extracted directly in the current directory, overwriting existing files if needed.\n\n"
            f"📍 Destination Path: <code>root/{USER_STATES[user_id].get('fm_path', '')}</code>\n\n"
            "Waiting for ZIP file upload...",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"fm_{bot_id}_refresh")]])
        )
        await query.answer()
        
    elif data.startswith("fm_zip_"):
        bot_id = int(data.split("_")[2])
        
        conn = get_db_connection()
        bot = conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
        conn.close()
        
        await query.answer("Generating backup ZIP archive...")
        zip_path = os.path.join("logs", f"bot_{bot_id}_backup.zip")
        
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(bot["folder"]):
                    for file in files:
                        file_full = os.path.join(root, file)
                        arcname = os.path.relpath(file_full, bot["folder"])
                        zipf.write(file_full, arcname)
                        
            await context.bot.send_document(
                chat_id=user_id,
                document=open(zip_path, 'rb'),
                filename=f"bot_{bot_id}_backup.zip",
                caption=f"📦 <b>Backup ZIP</b>\n\nBot: <code>{bot['name']}</code>\nCreated: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>",
                parse_mode="HTML"
            )
            os.remove(zip_path)
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to build backup zip: {e}")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/allow &lt;user_id&gt; [max_bots]</code>", parse_mode="HTML")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numerical User ID.")
        return
        
    max_bots = 1
    if len(context.args) > 1:
        try:
            max_bots = int(context.args[1])
        except ValueError:
            pass
            
    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (target_id,)).fetchone()
        if existing:
            conn.execute("UPDATE allowed_users SET max_bots = ? WHERE user_id = ?", (max_bots, target_id))
        else:
            conn.execute("INSERT OR IGNORE INTO allowed_users (user_id, max_bots) VALUES (?, ?)", (target_id, max_bots))
        conn.commit()
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> has been authorized with a limit of <b>{max_bots} bot slot(s)</b>!",
            parse_mode="HTML"
        )
        # Send direct notification to target user
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 <b>Bot Unlocked Now!</b>\n\n"
                    f"Your VPS hosting slot has been authorized by the Admin.\n"
                    f"Slots Limit: <b>{max_bots} bot slot(s)</b>.\n"
                    f"You can now use the 📤 <b>Upload Bot</b> button in the menu to host your bot! Enjoy!"
                ),
                parse_mode="HTML"
            )
        except Exception as ne:
            logger.warning(f"Could not notify unlocked user {target_id}: {ne}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to authorize user: {e}")
    finally:
        conn.close()

async def disallow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("💡 Usage: <code>/disallow &lt;user_id&gt;</code>", parse_mode="HTML")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid numerical User ID.")
        return
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (target_id,))
        conn.commit()
        await update.message.reply_text(
            f"❌ User <code>{target_id}</code> permission has been revoked.",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to revoke permission: {e}")
    finally:
        conn.close()

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    conn = get_db_connection()
    users = conn.execute("SELECT user_id, allowed_at FROM allowed_users ORDER BY allowed_at DESC").fetchall()
    conn.close()
    if not users:
        await update.message.reply_text("👥 No extra users are currently authorized.")
        return
    user_list = "👥 <b>AUTHORIZED USERS LIST</b> 👥\n━━━━━━━━━━━━━━━━━━━━━\n"
    for idx, u in enumerate(users, 1):
        user_list += f"{idx}. ID: <code>{u['user_id']}</code> (Added: {u['allowed_at']})\n"
    await update.message.reply_text(user_list, parse_mode="HTML")

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
        
    msg = await update.message.reply_text("📦 <b>Creating full system backup... Please wait.</b>", parse_mode="HTML")
    success, backup_path = create_system_backup()
    
    if success:
        try:
            with open(backup_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="gameover_hosting_backup.zip",
                    caption=(
                        "📦 <b>GAMEOVER HOSTING FULL BACKUP</b> 📦\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "This ZIP archive contains all databases, active bot files, and environment settings.\n\n"
                        "🖥️ <b>Local PC Setup Guide</b>:\n"
                        "1. Download and install <b>Python 3.10+</b> on your PC.\n"
                        "2. Extract this ZIP file into a folder.\n"
                        "3. Open your terminal in that folder and run:\n"
                        "   <code>pip install -r requirements.txt</code>\n"
                        "4. Run the main script:\n"
                        "   <code>python hosting.py</code>\n\n"
                        "🔄 <i>The bot daemon will automatically check and start all running uploader bots on your PC!</i>"
                    ),
                    parse_mode="HTML"
                )
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ <b>Backup created successfully but failed to send:</b> {e}", parse_mode="HTML")
    else:
        await msg.edit_text(f"❌ <b>Failed to create system backup:</b> {backup_path}", parse_mode="HTML")

def restart_hosting_bot():
    logger.info("Restarting hosting bot process...")
    try:
        lock_socket.close()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def post_init(application):
    # Set bot command menu
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "🚀 Start the hosting dashboard"),
            BotCommand("users", "👥 List authorized users (Admin Only)"),
            BotCommand("allow", "✅ Authorize a user ID (Admin Only)"),
            BotCommand("disallow", "❌ Revoke user authorization (Admin Only)"),
            BotCommand("backup", "📦 Generate full system backup (Admin Only)")
        ])
        logger.info("Bot commands menu successfully registered.")
    except Exception as e:
        logger.error(f"Failed to register commands menu: {e}")
        
    # Start auto-restart background task
    asyncio.create_task(auto_restart_daemon(application.bot))
    logger.info("Auto-restart background daemon activated.")


# ═══════════════════════════════════════════════════════════════════════
# Hugging Face Spaces — Port 7860 Anti-Timeout Shield
# Hugging Face requires a web server on port 7860. Without this,
# HF force-kills the entire container (and all hosted child bots)
# after 30 minutes of inactivity.
# ═══════════════════════════════════════════════════════════════════════

async def start_hf_health_server():
    """Start a lightweight aiohttp web server on port 7860 to keep
    the Hugging Face Spaces container permanently alive."""
    if not AIOHTTP_AVAILABLE:
        logger.warning("aiohttp not installed — skipping HF health-check server on port 7860.")
        return

    async def handle_ping(request):
        return aiohttp_web.Response(
            text="🟢 GAMEOVER VPS Hosting Panel is 24/7 Active & Running!"
        )

    app = aiohttp_web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)

    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", 7860)
    await site.start()
    logger.info("✅ Hugging Face Health Check Server started on port 7860")


def main():
    if not BOT_TOKEN:
        logger.error("No TELEGRAM_BOT_TOKEN set in environment variables.")
        sys.exit(1)

    logger.info("Initializing Premium Telegram Hosting Manager (Pure HTTP Bot API)...")

    # ── Startup Network Diagnostics ──────────────────────────────────
    import socket
    logger.info("[Diagnostics] Running initial connection check...")
    try:
        ip = socket.gethostbyname("api.telegram.org")
        logger.info(f"[Diagnostics] DNS Resolution OK: api.telegram.org -> {ip}")
        s = socket.create_connection(("api.telegram.org", 443), timeout=10)
        logger.info("[Diagnostics] TCP Connection to api.telegram.org:443 OK")
        s.close()
    except Exception as ne:
        logger.warning(f"[Diagnostics] Initial TCP/DNS connection check failed: {ne}")

    from telegram.request import HTTPXRequest
    # Set generous 30s timeouts to accommodate Hugging Face network startup lag
    request_config = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    
    custom_base_url = os.getenv("TELEGRAM_API_BASE_URL")
    if custom_base_url:
        custom_base_url = custom_base_url.rstrip("/")
        if not custom_base_url.endswith("/bot"):
            custom_base_url = custom_base_url + "/bot"
        logger.info(f"Setting custom Telegram API base URL: {custom_base_url}")
        application = ApplicationBuilder().token(BOT_TOKEN).base_url(custom_base_url).request(request_config).post_init(post_init).build()
    else:
        application = ApplicationBuilder().token(BOT_TOKEN).request(request_config).post_init(post_init).build()

    # ── Hugging Face Port 7860 — start health server BEFORE polling ──
    # We get the current event loop (or create one) and bind the health
    # server to it first. run_polling() then reuses the same loop, so
    # both coroutines share it and neither blocks the other.
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_hf_health_server())

    # ── Register all bot handlers ────────────────────────────────────
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("allow", allow_cmd))
    application.add_handler(CommandHandler("disallow", disallow_cmd))
    application.add_handler(CommandHandler("users", users_cmd))
    application.add_handler(CommandHandler("backup", backup_cmd))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, file_upload_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), menu_text_handler))

    # ── Robust Application Initialization Retry Loop ──────────────────
    initialized = False
    for attempt in range(1, 7):
        try:
            logger.info(f"Initializing Telegram Application (Attempt {attempt}/6)...")
            loop.run_until_complete(application.initialize())
            initialized = True
            logger.info("✅ Telegram Bot Application initialized successfully!")
            break
        except Exception as e:
            logger.error(f"⚠️ Initialization attempt {attempt}/6 failed: {e}")
            if attempt < 6:
                logger.info("Waiting 10 seconds for network to stabilize before retrying...")
                time.sleep(10)
            else:
                logger.critical("❌ All initialization attempts failed. Exiting.")
                sys.exit(1)

    logger.info("Telegram Bot Client started successfully.")

    # ── Start Telegram polling (runs forever) ────────────────────────
    application.run_polling()



if __name__ == '__main__':
    main()

