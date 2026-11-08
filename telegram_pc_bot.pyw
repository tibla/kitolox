import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
import shutil
import uuid
import ctypes
from ctypes import wintypes
from pathlib import Path

import mss
import psutil
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("telegram_pc_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = "8958827789:AAHXE9mVyOrbMCm2xAT7PeLhXHaJwDDtkak"
TELEGRAM_OWNER_ID = 1264872538

PAGE_SIZE = 8
PROTECTED_NAMES = {
    "system", "system idle process", "registry", "smss.exe", "csrss.exe",
    "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe",
}

# Идентификатор текущего ПК
def get_pc_id() -> str:
    try:
        return f"{platform.node()}_{uuid.getnode()}"
    except Exception:
        return "Default_PC"

CURRENT_PC_ID = get_pc_id()
PC_NAME = platform.node() or "Мой ПК"


def add_to_startup() -> None:
    if platform.system() != "Windows":
        return
    try:
        import winreg
        script_path = os.path.abspath(sys.argv[0])
        python_exe = sys.executable
        if "python.exe" in python_exe.lower():
            pythonw_exe = python_exe.lower().replace("python.exe", "pythonw.exe")
            if os.path.exists(pythonw_exe):
                python_exe = pythonw_exe

        command = f'"{python_exe}" "{script_path}"'
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "TelegramPCBot", 0, winreg.REG_SZ, command)
        logger.info("Успешно добавлено в автозагрузку.")
    except Exception:
        logger.exception("Не удалось добавить в автозагрузку")


def is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return user is not None and user.id == context.application.bot_data["owner_id"]


async def reject(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Доступ запрещён.")
    elif update.callback_query:
        await update.callback_query.answer("Доступ запрещён.", show_alert=True)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💻 ПК: {PC_NAME}", callback_data="select_pc")],
        [InlineKeyboardButton("📸 Скриншот", callback_data="screenshot"),
         InlineKeyboardButton("⏱ Время работы", callback_data="uptime")],
        [InlineKeyboardButton("📋 Диспетчер задач", callback_data="processes_0"),
         InlineKeyboardButton("🪟 Список окон", callback_data="windows_0")],
        [InlineKeyboardButton("📁 Проводник", callback_data="explorer_root")],
        [InlineKeyboardButton("🔄 Обновить код", callback_data="update_code"),
         InlineKeyboardButton("⏻ Выключить ПК", callback_data="shutdown")],
    ])


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await reject(update)
        return
    context.user_data["waiting_for_code"] = False
    message = update.effective_message
    if message:
        await message.reply_text(f"Управление компьютером ({PC_NAME}):", reply_markup=main_menu())


async def send_screenshot(message) -> None:
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "screen.png"
            with mss.mss() as screen:
                screen.shot(mon=0, output=str(image_path))
            with image_path.open("rb") as image:
                await message.reply_photo(photo=image)
    except Exception:
        logger.exception("Could not capture or send screenshot")
        await message.reply_text("Не удалось сделать скриншот.")


def get_windows() -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.EnumWindows.argtypes = [enum_windows_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    def collect(hwnd, _extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.strip()
            if title:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                result.append((title, pid.value))
        return True

    callback = enum_windows_proc(collect)
    user32.EnumWindows(callback, 0)
    return result


def process_label(pid: int, name: str) -> str:
    return f"{name[:22]} (PID {pid})"


def process_keyboard(items: list[tuple[int, str]], page_prefix: str,
                     page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(process_label(pid, name), callback_data=f"kill_{pid}")]
            for pid, name in items]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀", callback_data=f"{page_prefix}_page_{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton("▶", callback_data=f"{page_prefix}_page_{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("↩ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def show_processes(query, page: int) -> None:
    processes = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            processes.append((proc.info["pid"], proc.info["name"] or "(без имени)"))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    processes.sort(key=lambda item: item[1].casefold())
    total_pages = max(1, (len(processes) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    subset = processes[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    await query.edit_message_text(
        f"Диспетчер задач ({PC_NAME}) · процессов: {len(processes)} · страница {page + 1}/{total_pages}\n"
        "Выберите процесс, чтобы запросить его завершение:",
        reply_markup=process_keyboard(subset, "processes", page, total_pages),
    )


async def show_windows(query, page: int) -> None:
    windows = get_windows()
    total_pages = max(1, (len(windows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    subset = windows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    items = [(pid, title) for title, pid in subset]
    await query.edit_message_text(
        f"Открытые окна ({PC_NAME}): {len(windows)} · страница {page + 1}/{total_pages}\n"
        "Выберите окно, чтобы запросить завершение его процесса:",
        reply_markup=process_keyboard(items, "windows", page, total_pages),
    )


def uptime_text() -> str:
    seconds = int(time.time() - psutil.boot_time())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"Компьютер ({PC_NAME}) работает: {days} дн. {hours} ч. {minutes} мин."


# --- Логика проводника и архивации ---
async def show_explorer(query, path_str: str = "") -> None:
    if not path_str or path_str == "root":
        # Показываем список доступных дисков на Windows
        drives = [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        keyboard_rows = [[InlineKeyboardButton(f"💽 Диск {d}", callback_data=f"exp_path_{d}:\\")] for d in drives]
        keyboard_rows.append([InlineKeyboardButton("↩ В меню", callback_data="menu")])
        await query.edit_message_text(f"📁 Выберите диск на ПК **{PC_NAME}**:", reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode="Markdown")
        return

    target_path = Path(path_str)
    if not target_path.exists() or not target_path.is_dir():
        await query.edit_message_text("❌ Путь не найден или это файл.", reply_markup=main_menu())
        return

    try:
        items = sorted(list(target_path.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        await query.edit_message_text("❌ Ошибка доступа к папке.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Назад", callback_data=f"exp_path_{target_path.parent}")]]))
        return

    rows = []
    # Кнопка на уровень выше
    if target_path.parent != target_path:
        rows.append([InlineKeyboardButton("⬆️ На уровень выше", callback_data=f"exp_path_{target_path.parent}")])

    # Выводим папки и файлы (ограничим отображение, чтобы не заспамить клавиатуру)
    for item in items[:15]:
        if item.is_dir():
            rows.append([InlineKeyboardButton(f"📁 {item.name[:25]}", callback_data=f"exp_path_{item}")])
        else:
            # Для файлов даем возможность скачать или заархивировать
            rows.append([
                InlineKeyboardButton(f"📄 {item.name[:18]}", callback_data=f"exp_file_{item}"),
                InlineKeyboardButton("📦 ZIP", callback_data=f"exp_zip_{item}")
            ])

    rows.append([InlineKeyboardButton("📦 Заархивировать всю папку в ZIP", callback_data=f"exp_zip_{target_path}")])
    rows.append([InlineKeyboardButton("↩ В меню", callback_data="menu")])
    
    await query.edit_message_text(f"📁 Путь: `{target_path}`", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


async def handle_explorer_file(query, file_path_str: str) -> None:
    path = Path(file_path_str)
    if not path.exists():
        await query.answer("Файл не найден!", show_alert=True)
        return
    
    await query.answer("Отправка файла...")
    try:
        if path.is_file():
            await query.message.reply_document(document=open(path, "rb"))
        else:
            await query.message.reply_text("Это папка. Нажмите кнопку архивации папки для скачивания.")
    except Exception as e:
        await query.message.reply_text(f"❌ Не удалось отправить файл: {e}")


async def handle_explorer_zip(query, path_str: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = Path(path_str)
    if not path.exists():
        await query.answer("Объект не найден!", show_alert=True)
        return

    await query.edit_message_text(f"📦 Создаю архив для `{path.name}`... Пожалуйста, подождите.", parse_mode="Markdown")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_name = Path(temp_dir) / path.name
            # Создаем ZIP архив с помощью встроенного shutil
            archive_path = shutil.make_archive(str(archive_name), 'zip', path.parent if path.is_file() else path, path.name if path.is_file() else None)
            
            await query.message.reply_document(document=open(archive_path, "rb"), caption=f"📦 Архив: {path.name}.zip")
            await show_explorer(query, str(path.parent if path.is_dir() else path.parent))
    except Exception as e:
        logger.exception("Could not create archive")
        await query.message.reply_text(f"❌ Ошибка при создании архива: {e}", reply_markup=main_menu())


async def process_confirmation(query, pid: int) -> None:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        await query.edit_message_text("Процесс уже завершён или недоступен.", reply_markup=main_menu())
        return
    if pid in (0, 4, os.getpid()) or name.casefold() in PROTECTED_NAMES:
        await query.edit_message_text(
            f"Процесс {name} (PID {pid}) защищён от завершения ботом.", reply_markup=main_menu()
        )
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, завершить", callback_data=f"confirmkill_{pid}"),
         InlineKeyboardButton("Отмена", callback_data="menu")]
    ])
    await query.edit_message_text(
        f"Завершить процесс {name} (PID {pid})? Несохранённые данные приложения могут потеряться.",
        reply_markup=keyboard,
    )


async def terminate_process(query, pid: int) -> None:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        if pid in (0, 4, os.getpid()) or name.casefold() in PROTECTED_NAMES:
            await query.edit_message_text("Этот системный процесс защищён.", reply_markup=main_menu())
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
            result = f"Процесс {name} (PID {pid}) завершён."
        except psutil.TimeoutExpired:
            result = f"Процесс {name} (PID {pid}) получил запрос на завершение, но пока работает."
        await query.edit_message_text(result, reply_markup=main_menu())
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        await query.edit_message_text(f"Не удалось завершить процесс: {exc}.", reply_markup=main_menu())


async def schedule_shutdown(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, выключить через 30 сек.", callback_data="shutdown_confirm"),
        InlineKeyboardButton("Отмена", callback_data="menu"),
    ]])
    await query.edit_message_text("Подтвердите выключение компьютера:", reply_markup=keyboard)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update, context):
        await reject(update)
        return
    await query.answer()
    data = query.data or ""

    context.user_data["waiting_for_code"] = False

    if data == "menu":
        await query.edit_message_text(f"Управление компьютером ({PC_NAME}):", reply_markup=main_menu())
    elif data == "select_pc":
        # Мульти-ПК выбор (на текущий момент у нас один активный инстанс скрипта на данном ПК)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🟢 Текущий: {PC_NAME} (Активен)", callback_data="menu")],
            [InlineKeyboardButton("↩ В меню", callback_data="menu")]
        ])
        await query.edit_message_text(f"🖥 Выбор компьютера:\nСписок подключенных ПК с установленным ботом:", reply_markup=keyboard)
    elif data == "screenshot":
        await send_screenshot(query.message)
    elif data == "uptime":
        await query.edit_message_text(uptime_text(), reply_markup=main_menu())
    elif data == "explorer_root":
        await show_explorer(query, "root")
    elif data.startswith("exp_path_"):
        await show_explorer(query, data.replace("exp_path_", "", 1))
    elif data.startswith("exp_file_"):
        await handle_explorer_file(query, data.replace("exp_file_", "", 1))
    elif data.startswith("exp_zip_"):
        await handle_explorer_zip(query, data.replace("exp_zip_", "", 1), context)
    elif data == "update_code":
        context.user_data["waiting_for_code"] = True
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="menu")]])
        await query.edit_message_text(
            "📥 Отправьте мне новый файл с кодом (`.py`) прямо в этот чат, и бот обновится.",
            reply_markup=keyboard,
        )
    elif data == "shutdown":
        await schedule_shutdown(query, context)
    elif data == "shutdown_confirm":
        try:
            subprocess.run(["shutdown", "/s", "/t", "30"], check=True,
                           capture_output=True, text=True, timeout=10)
            context.application.bot_data["shutdown_scheduled"] = True
            await query.edit_message_text(
                "Компьютер выключится через 30 секунд. Отменить можно кнопкой /cancel.",
                reply_markup=main_menu(),
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not schedule shutdown")
            await query.edit_message_text("Не удалось запланировать выключение.", reply_markup=main_menu())
    elif data.startswith("processes_page_"):
        await show_processes(query, int(data.rsplit("_", 1)[1]))
    elif data.startswith("processes_"):
        await show_processes(query, int(data.rsplit("_", 1)[1]))
    elif data.startswith("windows_page_"):
        await show_windows(query, int(data.rsplit("_", 1)[1]))
    elif data.startswith("windows_"):
        await show_windows(query, int(data.rsplit("_", 1)[1]))
    elif data.startswith("kill_"):
        await process_confirmation(query, int(data.rsplit("_", 1)[1]))
    elif data.startswith("confirmkill_"):
        await terminate_process(query, int(data.rsplit("_", 1)[1]))
    else:
        await query.edit_message_text("Неизвестная команда.", reply_markup=main_menu())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await reject(update)
        return

    if not context.user_data.get("waiting_for_code", False):
        return

    document = update.effective_message.document
    if not document or not document.file_name.endswith(".py"):
        await update.effective_message.reply_text("Пожалуйста, отправьте файл с расширением .py")
        return

    try:
        await update.effective_message.reply_text("🔄 Скачиваю новый код и обновляюсь...")
        file = await context.bot.get_file(document.file_id)
        current_script_path = os.path.abspath(sys.argv[0])
        
        temp_file_path = current_script_path + ".new"
        await file.download_to_drive(temp_file_path)

        os.replace(temp_file_path, current_script_path)
        await update.effective_message.reply_text("✅ Бот успешно обновлен и перезапускается!")
        
        python_exe = sys.executable
        if "python.exe" in python_exe.lower():
            pythonw_exe = python_exe.lower().replace("python.exe", "pythonw.exe")
            if os.path.exists(pythonw_exe):
                python_exe = pythonw_exe

        subprocess.Popen([python_exe, current_script_path])
        os._exit(0)
    except Exception:
        logger.exception("Could not update bot code")
        await update.effective_message.reply_text("❌ Ошибка при обновлении кода.")


async def cancel_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await reject(update)
        return
    if not context.application.bot_data.get("shutdown_scheduled", False):
        await update.effective_message.reply_text("Бот не планировал выключение.", reply_markup=main_menu())
        return
    try:
        subprocess.run(["shutdown", "/a"], check=True, capture_output=True, text=True, timeout=10)
        context.application.bot_data["shutdown_scheduled"] = False
        await update.effective_message.reply_text("Выключение отменено.", reply_markup=main_menu())
    except (OSError, subprocess.SubprocessError):
        logger.exception("Could not cancel shutdown")
        await update.effective_message.reply_text("Не удалось отменить выключение.", reply_markup=main_menu())


async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update, context):
        await reject(update)
        return
    await send_screenshot(update.effective_message)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.error(
            "Telegram returned 409 Conflict: another copy is polling with this token. "
            "Close the other bot process and start only one copy."
        )
        return
    logger.error("Bot update failed: %s", context.error)


def main() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("Этот пример поддерживает только Windows.")
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_ID <= 0:
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_ID в начале файла.")
    
    add_to_startup()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN.strip()).build()
    application.bot_data["owner_id"] = TELEGRAM_OWNER_ID
    application.bot_data["shutdown_scheduled"] = False
    
    application.add_handler(CommandHandler("start", send_menu))
    application.add_handler(CommandHandler("menu", send_menu))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("cancel", cancel_shutdown))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_error_handler(on_error)
    
    logger.info("Bot started; only the configured owner can use it.")
    
    # Explicitly ensure an event loop exists for the main thread (fixes pythonw / .pyw loop issues)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    application.run_polling(allowed_updates=Update.ALL_TYPES)
    
if __name__ == "__main__":
    main()
