from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import users_bd
import backends
from werkzeug.security import generate_password_hash, check_password_hash
import os
import sys
import uuid
import time
import shutil

# =========================================
# КОДИРОВКА ВЫВОДА В КОНСОЛЬ
# В Windows консоль по умолчанию использует cp866/cp1251, из-за чего
# print() с эмодзи падает с UnicodeEncodeError и сервер не стартует.
# Переключаем стандартный вывод на UTF-8 (с заменой неподдерживаемых
# символов, чтобы это не могло уронить приложение).
# =========================================
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass

# =========================================
# УВЕЛИЧИВАЕМ ТАЙМАУТЫ httpx
# =========================================
import httpx

httpx._config.DEFAULT_TIMEOUT_CONFIG = httpx.Timeout(300.0, connect=60.0)

_orig_get = httpx.get
def _patched_get(*args, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = 300.0
    return _orig_get(*args, **kwargs)
httpx.get = _patched_get

_orig_client_init = httpx.Client.__init__
def _patched_client_init(self, *args, **kwargs):
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = httpx.Timeout(300.0, connect=60.0)
    _orig_client_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_client_init

os.environ["GRADIO_CLIENT_TIMEOUT"] = "300"


# =========================================
# Импорт gradio_client
# =========================================
try:
    import gradio_client  # noqa: F401  (клиенты создаются в backends.py)
    GRADIO_AVAILABLE = True
except ImportError:
    GRADIO_AVAILABLE = False
    print("⚠ gradio_client не установлен. Выполните: pip install gradio_client")


# =========================================
# HF токен
# =========================================
try:
    from dotenv import load_dotenv
    _env_dir = os.path.dirname(os.path.abspath(__file__))
    _env_root = os.path.abspath(os.path.join(_env_dir, '..'))
    _env_path = os.path.join(_env_root, '.env')
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
        print(f"📄 Загружен .env из {_env_path}")
    else:
        print(f"⚠ Файл .env не найден по пути {_env_path}")
except ImportError:
    print("⚠ python-dotenv не установлен")

HF_TOKEN = os.getenv('HF_TOKEN')
if HF_TOKEN:
    print(f"✅ Hugging Face токен загружен (начинается с {HF_TOKEN[:6]}...)")
else:
    print("ℹ️  Hugging Face токен не задан (работаем без авторизации)")


app = Flask(__name__)
CORS(app)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    return response


# =========================================
# ПУТИ
# =========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))
UPLOADS_DIR = os.path.join(PROJECT_ROOT, 'uploads')
IMAGES_DIR = os.path.join(PROJECT_ROOT, 'images')

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

UPLOAD_FOLDER = IMAGES_DIR
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_photo(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# =========================================
# СТАТИКА
# =========================================

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOADS_DIR, filename)


@app.route('/images/<path:filename>')
def serve_images(filename):
    return send_from_directory(IMAGES_DIR, filename)


# =========================================
# ДВИЖКИ ПРИМЕРКИ
#
# Сами движки — в backends.py, здесь только настройка порядка.
# По умолчанию: сначала IDM-VTON (настоящая примерка по двум фото),
# затем Qwen-Image-Edit, затем Kolors.
# =========================================

TRYON = {
    # Порядок попыток. Уберите лишнее, чтобы не ждать зря.
    "backends": [
        {"id": "idm_vton", "timeout": 420, "client_timeout": 600},
        {"id": "qwen_edit", "timeout": 900, "client_timeout": 900},
        # Kolors: автор Space закрыл внешний API, поэтому по умолчанию выключен.
        # Включите, если Space снова откроет API.
        # {"id": "kolors", "timeout": 420, "client_timeout": 600},
    ],
    "steps": 30,          # число шагов генерации
    "seed": 42,           # фиксированное зерно (одинаковый результат)
    "retries": 1,         # сколько раз повторить весь список движков
    # Таймаут подключения к Space (запрос /config). Держим небольшим,
    # чтобы нерабочий Space не задерживал переход к следующему движку.
    "connect_timeout": 90,
}



# =========================================
# РЕГИСТРАЦИЯ / ЛОГИН
# =========================================

@app.route('/api/save', methods=['POST'])
def save_data():
    data = request.get_json()
    name = data.get('name')
    regmail = data.get('email')
    regpass = data.get('password')
    pass_hash = generate_password_hash(regpass)
    gender = data.get('gender')
    date = data.get('date')

    success = users_bd.add_user(name, regmail, pass_hash, gender, date)

    if success:
        return jsonify({"status": "ok", "message": "Регистрация успешна"})
    return jsonify({
        "status": "error",
        "message": "Ошибка БД: возможно, пользователь с таким Email или Логином уже существует"
    }), 400


@app.route('/api/login', methods=['POST'])
def login_data():
    data = request.get_json()
    mail_log = data.get('mail')
    pass_log = data.get('pass')

    if mail_log == 'admin' and pass_log == 'admin':
        return jsonify({"status": "ok", "message": "Вход выполнен", "role": "admin"}), 200

    bd_data = users_bd.get_user(mail_log)

    if bd_data:
        correct = check_password_hash(bd_data['password'], pass_log)
        if correct:
            return jsonify({
                "status": "ok",
                "message": "Вход выполнен",
                "role": "user",
                "user_id": bd_data['id']
            }), 200
        return jsonify({"status": "error 1", "message": "Неверный пароль"}), 401
    return jsonify({"status": "error", "message": "Пользователь не найден"}), 404


# =========================================
# ТОВАРЫ
# =========================================

@app.route('/api/products', methods=['GET'])
def products_data():
    return jsonify(users_bd.get_product())


@app.route('/api/admin/product', methods=['POST'])
def product_add():
    if 'image' not in request.files:
        return jsonify({"status": "error", "message": "Файл картинки не найден"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"status": "error", "message": "Файл не выбран"}), 400

    if file and allowed_file(file.filename):
        ext = file.filename.rsplit('.', 1)[1].lower()
        unique_filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
        file.save(filepath)

        type = request.form.get('type')
        name = request.form.get('name')
        price = request.form.get('price')
        image = f"../images/{unique_filename}"

        success = users_bd.add_product(name, type, price, image)

        if success:
            return jsonify({"status": "ok", "message": "Товар успешно добавлен"}), 200
        return jsonify({"status": "error", "message": "Ошибка при добавлении в БД"}), 500
    return jsonify({"status": "error", "message": "Недопустимый формат"}), 400


@app.route('/api/admin/products', methods=['GET'])
def get_all_products():
    return jsonify(users_bd.get_all_products())


@app.route('/api/admin/product/delete', methods=['POST'])
def delete_product():
    data = request.get_json()
    product_id = data.get('id')
    if not product_id:
        return jsonify({"status": "error", "message": "Не указан ID товара"}), 400

    success = users_bd.delete_product(product_id)
    if success:
        return jsonify({"status": "ok", "message": "Товар удален"}), 200
    return jsonify({"status": "error", "message": "Товар не найден"}), 404


# =========================================
# ИЗБРАННОЕ
# =========================================

@app.route('/api/wishlist', methods=['GET'])
def get_wishlist():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "Не указан user_id"}), 400
    return jsonify(users_bd.get_user_favorites(user_id))


@app.route('/api/wishlist/toggle', methods=['POST'])
def toggle_wishlist():
    data = request.get_json()
    user_id = data.get('user_id')
    product_id = data.get('product_id')
    if not user_id or not product_id:
        return jsonify({"status": "error", "message": "Не хватает данных"}), 400

    success = users_bd.toggle_favorite(user_id, product_id)
    if success:
        return jsonify({"status": "ok", "message": "Список обновлен"}), 200
    return jsonify({"status": "error", "message": "Ошибка БД"}), 500


# =========================================
# ПАРАМЕТРЫ ПОЛЬЗОВАТЕЛЯ
# =========================================

@app.route('/api/user/data', methods=['GET', 'POST'])
def handle_user_data():
    if request.method == 'GET':
        user_id = request.args.get('user_id')
        if not user_id:
            return jsonify({"status": "error", "message": "Не указан user_id"}), 400
        data = users_bd.get_user_data(user_id)
        return jsonify(data if data else {}), 200

    data = request.get_json()
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "Не указан user_id"}), 400

    success = users_bd.save_user_data(
        user_id=user_id,
        height=data.get('height'),
        weight=data.get('weight'),
        shoe_size=data.get('shoe_size'),
        clothes_size=data.get('clothes_size')
    )
    if success:
        return jsonify({"status": "ok", "message": "Данные сохранены"}), 200
    return jsonify({"status": "error", "message": "Ошибка БД"}), 500


# =========================================
# ЗАГРУЗКА ФОТО
# =========================================

@app.route('/api/upload/photos', methods=['POST'])
def upload_photos():
    user_id = request.form.get('user_id')
    photo_index = request.form.get('photo_index')

    if not user_id or not photo_index:
        return jsonify({"status": "error", "message": "Не хватает данных"}), 400

    user_upload_dir = os.path.join(UPLOADS_DIR, f'user_{user_id}')
    os.makedirs(user_upload_dir, exist_ok=True)

    file_key = f'photo_{photo_index}'
    if file_key not in request.files:
        return jsonify({"status": "error", "message": f"Файл {file_key} не найден"}), 400

    file = request.files[file_key]
    if file.filename == '':
        return jsonify({"status": "error", "message": "Файл не выбран"}), 400

    if not allowed_photo(file.filename):
        return jsonify({"status": "error", "message": f"Недопустимый формат: {file.filename}"}), 400

    ext = file.filename.rsplit('.', 1)[1].lower()
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(user_upload_dir, unique_filename)
    file.save(filepath)

    relative_path = f"uploads/user_{user_id}/{unique_filename}"
    success = users_bd.update_user_photo(user_id, int(photo_index), relative_path)

    if success:
        return jsonify({"status": "ok", "message": "Фото сохранено", "path": relative_path}), 200
    return jsonify({"status": "error", "message": "Ошибка БД"}), 500


@app.route('/api/user/photos', methods=['GET'])
def get_user_photos_endpoint():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "Не указан user_id"}), 400
    return jsonify(users_bd.get_user_photos(user_id)), 200


# =========================================
# ПРОГРЕСС ПРИМЕРКИ
#
# Несколько вещей надеваются по очереди (движки принимают только
# одно фото одежды за вызов), поэтому примерка из 3 вещей — это
# 3 генерации подряд. Фронтенд опрашивает прогресс, чтобы показать
# пользователю, на каком шаге он находится.
# =========================================
PROGRESS = {}


def _set_progress(user_id, **kwargs):
    state = PROGRESS.setdefault(str(user_id), {})
    state.update(kwargs)
    state['updated'] = time.time()


def _clear_progress(user_id):
    PROGRESS.pop(str(user_id), None)


def _get_progress(user_id):
    return PROGRESS.get(str(user_id), {})


# =========================================
# ВИРТУАЛЬНАЯ ПРИМЕРКА
#
# Логика самих движков — в backends.py.
# Каждый движок надевает ОДНУ вещь за вызов, поэтому выбранные вещи
# обрабатываются по очереди: результат предыдущего шага становится
# «человеком» для следующего. Итог — человек во всей выбранной одежде.
# =========================================

def _collect_products(product_ids):
    """Готовит список товаров для примерки. Возвращает (items, error)."""
    items = []
    for pid in product_ids:
        product = users_bd.get_product_by_id(pid)
        if not product or not product.get('image'):
            continue

        rel = product['image'].replace('../', '')
        full_path = os.path.join(PROJECT_ROOT, rel)
        if not os.path.exists(full_path):
            return None, f"Файл одежды не найден: {full_path}"

        items.append({
            "path": full_path,
            "name": product.get('name') or "clothing item",
            "type": product.get('type') or "clothing",
            "description": backends.describe_garment(product),
        })
    return items, None


def _generate_tryon_internal(user_id, source_photo, product_ids):
    """
    Надевает все выбранные вещи по очереди.

    Вещи уходят отдельными запросами, потому что у каждого Space ровно
    один вход под фото одежды:
      IDM-VTON      — imgs (человек) + garm_img (одна вещь)
      Qwen-Image-Edit — одно фото + текст с описанием вещи
      Kolors        — person_img + garment_img (одна вещь)

    Результат шага N подаётся как «человек» на шаг N+1, поэтому в итоге
    человек оказывается во всей выбранной одежде.
    Сохраняются все промежуточные шаги (step1, step2, ...) и итог.

    Возвращает (success, result_path, error, backend_id).
    """
    if not GRADIO_AVAILABLE:
        return False, None, "gradio_client не установлен", None

    person_path = os.path.join(PROJECT_ROOT, source_photo)
    if not os.path.exists(person_path):
        return False, None, f"Фото человека не найдено: {person_path}", None

    garments, error = _collect_products(product_ids)
    if error:
        return False, None, error, None
    if not garments:
        return False, None, "Нет товаров для примерки", None

    total = len(garments)
    print(f"👕 Вещей выбрано: {total} — будут надеты по очереди")
    for i, g in enumerate(garments, 1):
        print(f"   {i}. {g['name']} ({g['type']})")

    user_result_dir = os.path.join(UPLOADS_DIR, f'user_{user_id}')
    os.makedirs(user_result_dir, exist_ok=True)

    errors = []

    # ---- перебираем движки, пока какой-нибудь не выполнит ВСЕ шаги ----
    for cfg in TRYON["backends"]:
        backend_id = cfg["id"]
        space = backends.BACKENDS.get(backend_id, {}).get("space", "?")
        print(f"\n🔁 Движок: {backend_id} ({space})")

        current_person = person_path
        saved_steps = []
        failed = False

        for index, garment in enumerate(garments, 1):
            prompt = backends.build_tryon_prompt([garment["description"]],
                                                 index=index, total=total)
            _set_progress(
                user_id,
                active=True,
                backend=backend_id,
                step=index,
                total=total,
                garment=garment["name"],
                message=f"Шаг {index} из {total}: надеваю «{garment['name']}»",
            )
            print(f"\n   Шаг {index}/{total}: {garment['name']}")
            print(f"   Промпт: {prompt}")

            options = {
                "prompt": prompt,
                "steps": int(cfg.get("steps", TRYON["steps"])),
                "seed": int(cfg.get("seed", TRYON["seed"])),
                "timeout": cfg.get("timeout", 420),
                "connect_timeout": cfg.get("connect_timeout", TRYON["connect_timeout"]),
                "token": HF_TOKEN,
            }

            ok, result_path, err = backends.run_backend(
                backend_id, current_person, garment, options
            )

            if not ok:
                print(f"   ❌ Шаг {index} не удался: {err}")
                errors.append(f"{backend_id} (шаг {index}, {garment['name']}): {err}")
                failed = True
                break

            step_name = (f"step{index}_{uuid.uuid4().hex}.png" if total > 1
                         else f"gen_{uuid.uuid4().hex}.png")
            step_path = os.path.join(user_result_dir, step_name)
            try:
                shutil.copy(result_path, step_path)
            except Exception as e:
                errors.append(f"{backend_id} (шаг {index}): не удалось сохранить: {e}")
                failed = True
                break

            saved_steps.append(step_path)
            current_person = step_path  # следующий шаг надеваем поверх этого
            print(f"   ✅ Шаг {index} сохранён: {step_name} "
                  f"({os.path.getsize(step_path)} байт)")

        if failed or not saved_steps:
            continue

        # ---- успех: переименовываем итог в gen_*.png ----
        final_name = f"gen_{uuid.uuid4().hex}.png"
        final_path = os.path.join(user_result_dir, final_name)
        shutil.copy(saved_steps[-1], final_path)

        if total > 1:
            print(f"✅ Итог после {total} вещей: {final_name}")
        else:
            print(f"✅ Готово: {final_name}")

        return True, f"uploads/user_{user_id}/{final_name}", None, backend_id

    return False, None, (" | ".join(errors) if errors else "Неизвестная ошибка"), None



@app.route('/api/generate/tryon', methods=['POST'])
def generate_tryon():
    data = request.get_json()
    user_id = data.get('user_id')
    source_photo = data.get('source_photo')
    product_ids = data.get('product_ids', [])

    if not user_id or not source_photo or not product_ids:
        return jsonify({"status": "error", "message": "Не хватает данных"}), 400

    if not GRADIO_AVAILABLE:
        return jsonify({
            "status": "error",
            "message": "gradio_client не установлен. Выполните: pip install gradio_client"
        }), 500

    max_attempts = max(1, int(TRYON.get("retries", 1)))
    last_error = "Неизвестная ошибка"

    _set_progress(user_id, active=True, step=0, total=len(product_ids),
                  message="Готовлю примерку...")

    try:
        for attempt in range(1, max_attempts + 1):
            print(f"🔄 Попытка {attempt}/{max_attempts} для user_id={user_id}")

            success, result_path, error, backend_id = _generate_tryon_internal(
                user_id=user_id,
                source_photo=source_photo,
                product_ids=product_ids
            )

            if success:
                gen_id = users_bd.save_generation(
                    user_id=user_id,
                    source_photo=source_photo,
                    product_ids=product_ids,
                    result_path=result_path
                )

                if gen_id:
                    print(f"✅ Примерка успешна (id={gen_id}, движок={backend_id})")
                    return jsonify({
                        "status": "ok",
                        "message": "Примерка выполнена",
                        "generation_id": gen_id,
                        "result": result_path,
                        "backend": backend_id,
                        "garments_applied": len(product_ids)
                    }), 200
                return jsonify({"status": "error", "message": "Не удалось сохранить в БД"}), 500

            last_error = error
            print(f"❌ Попытка {attempt} провалилась: {error}")

            if attempt < max_attempts:
                print("⏸ Пауза 3 сек...")
                time.sleep(3)

        return jsonify({
            "status": "error",
            "message": "Не удалось выполнить примерку",
            "details": last_error
        }), 500
    finally:
        _clear_progress(user_id)


@app.route('/api/generate/progress', methods=['GET'])
def generate_progress():
    """
    Прогресс примерки: сколько вещей уже надето.

    Несколько вещей надеваются по очереди, поэтому примерка из 3 вещей —
    это 3 генерации подряд. Страница опрашивает этот эндпоинт и показывает,
    на каком шаге она находится.
    """
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "Не указан user_id"}), 400
    return jsonify(_get_progress(user_id)), 200




# =========================================
# СОХРАНЁННЫЕ ГЕНЕРАЦИИ
# =========================================

@app.route('/api/generations', methods=['GET'])
def get_generations():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"status": "error", "message": "Не указан user_id"}), 400
    return jsonify(users_bd.get_user_generations(user_id)), 200


@app.route('/api/generations/delete', methods=['POST'])
def delete_generation():
    data = request.get_json()
    gen_id = data.get('id')
    if not gen_id:
        return jsonify({"status": "error", "message": "Не указан ID"}), 400

    success = users_bd.delete_generation(gen_id)
    if success:
        return jsonify({"status": "ok", "message": "Удалено"}), 200
    return jsonify({"status": "error", "message": "Не найдено"}), 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)