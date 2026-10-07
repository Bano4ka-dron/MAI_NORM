"""
Движки виртуальной примерки через Hugging Face Spaces.

Каждый движок — функция run(person_path, garments, options) -> путь к PNG.
Функции не бросают исключений наружу: возвращают (ok, result_path, error),
чтобы server.py мог переключиться на следующий движок.

Требуется gradio_client (см. server/requirements.txt).
"""

import os
import time

try:
    import httpx
    from gradio_client import Client, handle_file
    from gradio_client.exceptions import AppError
    GRADIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    GRADIO_AVAILABLE = False
    AppError = Exception


# =========================================================
# РЕЕСТР ДВИЖКОВ
#
# idm_vton — НАСТОЯЩАЯ примерка: принимает два фото
#            (человек + вещь) и надевает реальную вещь.
# qwen_edit — Qwen-Image-Edit: принимает ОДНО фото + текст.
#             Вещь описывается словами, фото товара не используется.
# kolors   — Kolors Virtual Try-On. У этого Space автор закрыл
#             внешний API (Tryon.queue(api_open=False)), поэтому
#             оставлен как запасной вариант.
# =========================================================

BACKENDS = {
    # Проверено по исходнику Space (app.py): входы
    # [imgs(ImageEditor), garm_img(Image), prompt, is_checked, is_checked_crop,
    #  denoise_steps, seed], api_name='tryon'
    "idm_vton": {
        "space": "yisol/IDM-VTON",
        "api_name": "/tryon",
        "kind": "two_image",
        "timeout": 420,
    },
    # Проверено через view_api: входы
    # [image, prompt, seed, randomize_seed, true_guidance_scale,
    #  num_inference_steps, rewrite_prompt], api_name='/infer'
    "qwen_edit": {
        "space": "Qwen/Qwen-Image-Edit",
        "api_name": "/infer",
        "kind": "one_image",
        "timeout": 900,
    },
    # Внимание: у Kolors API закрыт автором, вызов почти наверняка не пройдёт.
    "kolors": {
        "space": "Kwai-Kolors/Kolors-Virtual-Try-On",
        "api_name": "/tryon",
        "kind": "two_image",
        "timeout": 420,
    },
}

_clients = {}


def get_client(space, timeout=300.0, token=None, config_timeout=90.0):
    """
    Ленивое подключение к Space (один клиент на Space).

    config_timeout — отдельный небольшой таймаут для первичного запроса
    /config. Держим его коротким: если Space не отвечает, нужно быстро
    перейти к следующему движку, а не ждать минутами.
    """
    if not GRADIO_AVAILABLE:
        raise RuntimeError("gradio_client не установлен")
    if space not in _clients:
        kwargs = {}
        if token:
            kwargs["token"] = token
        try:
            _clients[space] = Client(
                space,
                httpx_kwargs={"timeout": httpx.Timeout(config_timeout, connect=30.0)},
                verbose=False,
                **kwargs,
            )
        except TypeError:
            # старая версия gradio_client не знает про token
            _clients[space] = Client(
                space,
                httpx_kwargs={"timeout": httpx.Timeout(config_timeout, connect=30.0)},
                verbose=False,
            )
    return _clients[space]


def extract_image_path(outputs):
    """
    Достаёт путь к картинке из ответа Gradio.
    Ответ бывает: str, dict, [(dict|str), ...], либо кортеж с несколькими выходами.
    """
    if outputs is None:
        return None

    if isinstance(outputs, str):
        return outputs

    if isinstance(outputs, (list, tuple)):
        for item in outputs:
            path = extract_image_path(item)
            if path:
                return path
        return None

    if isinstance(outputs, dict):
        for key in ("path", "url", "value"):
            v = outputs.get(key)
            if isinstance(v, str) and v:
                return v
        return None

    return None


def _describe_empty_job(job):
    """
    Объясняет, почему задача не дала результата.

    Самая частая причина у Space на ZeroGPU — исчерпанная квота:
    сервер отвечает ошибкой, но список результатов остаётся пустым,
    поэтому без разбора статуса причина выглядит непонятно.
    """
    text = ""
    try:
        text = str(job.status())
    except Exception:
        pass

    low = text.lower()
    if "zerogpu" in low or "quota" in low:
        return ("Исчерпана бесплатная квота ZeroGPU у аккаунта Hugging Face. "
                "Квота восстанавливается примерно через сутки. "
                "Подробнее: https://huggingface.co/settings/billing")
    if "aborted" in low:
        return f"Задача на GPU прервана сервером. Статус: {text[:200]}"
    if "starting" in low:
        return ("Space не начал выполнение (задача осталась в очереди). "
                "Вероятные причины: исчерпана квота ZeroGPU или Space выгружен "
                "из памяти. Попробуйте позже.")
    if text:
        return f"Space не вернул результат. Статус задачи: {text[:250]}"
    return "Space не вернул ни одного результата"


def _looks_like_placeholder(path):
    """
    Gradio отдаёт картинку-заглушку 'Generating image...' (~5 КБ),
    если задача очереди не выполнилась. Возвращаем True для подозрительно
    маленьких PNG, чтобы не сохранить заглушку как результат.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    return size < 15000 and path.lower().endswith(".png")


def wait_result(client, inputs, api_name, timeout):
    """
    Отправляет задачу и ждёт завершения.
    Возвращает (path, error). path=None при ошибке.
    """
    start = time.time()
    try:
        job = client.submit(api_name=api_name, **inputs)
    except AppError as e:
        # например: "You have exceeded your free ZeroGPU quota"
        return None, f"Space отклонил задачу: {str(e)[:300]}"
    except Exception as e:
        return None, f"Не удалось отправить задачу: {type(e).__name__}: {str(e)[:200]}"

    last_status = None
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            try:
                job.cancel()
            except Exception:
                pass
            return None, f"Превышено время ожидания ({timeout} с)"

        if job.done():
            break

        status = job.status()
        if status != last_status:
            print(f"   ⏳ {elapsed:5.0f} с — {status}")
            last_status = status
        time.sleep(2)

    try:
        outputs = job.outputs()
    except Exception as e:
        return None, f"Ошибка получения результата: {type(e).__name__}: {e}"

    if not outputs:
        return None, _describe_empty_job(job)

    path = extract_image_path(outputs[-1])
    if not path:
        return None, f"В ответе нет картинки: {str(outputs[-1])[:200]}"

    if not os.path.exists(path):
        # иногда приходит URL вместо локального файла
        if isinstance(path, str) and path.startswith("http"):
            try:
                r = httpx.get(path, timeout=120.0)
                r.raise_for_status()
                tmp = os.path.join(os.path.dirname(path) or ".", f"dl_{int(time.time())}.png")
                with open(tmp, "wb") as f:
                    f.write(r.content)
                path = tmp
            except Exception as e:
                return None, f"Не удалось скачать результат: {type(e).__name__}: {e}"
        else:
            return None, f"Файл результата не найден: {path}"

    if _looks_like_placeholder(path):
        return None, ("Space вернул картинку-заглушку 'Generating image...' "
                      "вместо результата — задача не выполнилась")

    return path, None


# =========================================================
# ОПИСАНИЕ ВЕЩИ ДЛЯ ПРОМПТА
# =========================================================

def describe_garment(product):
    """
    Собирает текстовое описание вещи из данных товара.

    В базе название часто уже включает тип ("куртка розовая"),
    поэтому тип добавляем только если его нет в названии.
    """
    name = (product.get("name") or "").strip()
    gtype = (product.get("type") or "").strip()

    if name and gtype:
        if gtype.lower() in name.lower():
            return name
        return f"{name} ({gtype})"
    return name or gtype or "clothing item"


def build_tryon_prompt(garments, index=None, total=None):
    """
    Промпт примерки.

    Один вызов движка надевает ОДНУ вещь: ни IDM-VTON, ни Qwen-Image-Edit
    не принимают несколько фото одежды за раз (у обоих ровно один вход
    под картинку одежды). Поэтому несколько вещей надеваются по очереди,
    а этот текст описывает текущий шаг.
    """
    if not garments:
        return "The person wearing the clothes from the reference images"

    if len(garments) == 1:
        desc = garments[0]
    else:
        desc = ", ".join(garments[:-1]) + f" and {garments[-1]}"

    if index is not None and total is not None and total > 1:
        return f"The person wearing {desc} (step {index} of {total})"
    return f"The person wearing {desc}"


# =========================================================
# ДВИЖОК: IDM-VTON (человек + вещь, настоящая примерка)
#
# Каждый вызов надевает ОДНУ вещь: у Space один вход под одежду
# (источник: app.py, компонент garm_img).
# =========================================================

def run_idm_vton(person_path, garment, options):
    """
    yisol/IDM-VTON.
    Вход imgs — компонент ImageEditor, ему нужен словарь:
        {"background": {path}, "layers": [], "composite": None}
    Клиент сам загрузит вложенные файлы (проверено: utils.traverse
    в паре с is_file_obj_with_meta обходит вложенные dict с 'path').
    """
    garment_path = garment["path"]

    client = get_client(BACKENDS["idm_vton"]["space"],
                        token=options.get("token"),
                        config_timeout=float(options.get("connect_timeout", 90)))

    inputs = {
        "dict": {
            "background": handle_file(person_path),
            "layers": [],
            "composite": None,
        },
        "garm_img": handle_file(garment_path),
        "garment_des": options["prompt"],
        "is_checked": True,       # авто-маска по позе человека
        "is_checked_crop": True,  # авто-обрезка под пропорции 3:4
        "denoise_steps": int(options.get("steps", 30)),
        "seed": int(options.get("seed", 42)),
    }

    path, error = wait_result(
        client, inputs, BACKENDS["idm_vton"]["api_name"],
        timeout=options.get("timeout", 420),
    )
    if error:
        return False, None, f"IDM-VTON: {error}"
    return True, path, None


# =========================================================
# ДВИЖОК: QWEN IMAGE EDIT (человек + описание вещи)
#
# Принимает ТОЛЬКО одно фото (проверено через /config: единственный
# вход image[6]). Вещь передаётся текстом в промпте.
# =========================================================

def run_qwen_edit(person_path, garment, options):
    """
    Qwen/Qwen-Image-Edit.
    rewrite_prompt=False: у Space нет DASH_API_KEY, встроенный
    переписыватель промпта зацикливается в бесконечных повторах.
    """
    client = get_client(BACKENDS["qwen_edit"]["space"],
                        token=options.get("token"),
                        config_timeout=float(options.get("connect_timeout", 90)))

    prompt = (
        f"Virtual try-on. Show this exact person wearing {garment['description']}. "
        "Replace only the clothing. Keep the person's face, hair, body shape and pose "
        "exactly unchanged. Photorealistic, natural fabric folds, studio lighting, full body."
    )

    inputs = {
        "image": handle_file(person_path),
        "prompt": prompt,
        "seed": int(options.get("seed", 42)),
        "randomize_seed": False,
        "true_guidance_scale": 4.0,
        "num_inference_steps": int(options.get("steps", 20)),
        "rewrite_prompt": False,
    }

    path, error = wait_result(
        client, inputs, BACKENDS["qwen_edit"]["api_name"],
        timeout=options.get("timeout", 900),
    )
    if error:
        return False, None, f"Qwen-Image-Edit: {error}"
    return True, path, None


# =========================================================
# ДВИЖОК: KOLORS (человек + вещь, API закрыт автором)
# =========================================================

def run_kolors(person_path, garment, options):
    """Kwai-Kolors/Kolors-Virtual-Try-On. Оставлен как запасной."""
    garment_path = garment["path"]

    client = get_client(BACKENDS["kolors"]["space"],
                        token=options.get("token"),
                        config_timeout=float(options.get("connect_timeout", 90)))

    inputs = {
        "person_img": handle_file(person_path),
        "garment_img": handle_file(garment_path),
        "seed": int(options.get("seed", 42)),
        "randomize_seed": False,
    }

    path, error = wait_result(
        client, inputs, BACKENDS["kolors"]["api_name"],
        timeout=options.get("timeout", 420),
    )
    if error:
        return False, None, f"Kolors: {error}"
    return True, path, None


# Определение движка: сколько фото одежды он принимает за один вызов.
RUNNERS = {
    "idm_vton": {"fn": run_idm_vton, "garments_per_call": 1},
    "qwen_edit": {"fn": run_qwen_edit, "garments_per_call": 1},
    "kolors": {"fn": run_kolors, "garments_per_call": 1},
}


def run_backend(backend_id, person_path, garment, options):
    """
    Один вызов движка: надевает ОДНУ вещь.
    Возвращает (ok, path, error).
    """
    entry = RUNNERS.get(backend_id)
    if entry is None:
        return False, None, f"Неизвестный движок: {backend_id}"
    try:
        return entry["fn"](person_path, garment, options)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, None, f"{backend_id}: {type(e).__name__}: {e}"
