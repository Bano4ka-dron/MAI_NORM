"""
Живая проверка примерки на реальных данных.

Запуск (из папки server):
    python test_tryon.py            # 2 вещи (по умолчанию)
    python test_tryon.py 1          # только 1 вещь

Вещи надеваются ПО ОЧЕРЕДИ: каждый движок принимает только одно фото
одежды за вызов, поэтому 2 вещи = 2 генерации подряд, а результат
первого шага становится «человеком» для второго.

ВАЖНО: у Space на ZeroGPU бесплатная квота ~110 секунд в сутки на аккаунт.
Одна примерка просит ~240 секунд, поэтому двух вещей на бесплатном тарифе
почти наверняка не хватит. Если квота исчерпана — все шаги вернут ошибку,
это нормально, просто запустите скрипт позже.
"""

import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backends
import server as srv


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    print("=" * 70)
    print(f"ПРОВЕРКА ПРИМЕРКИ ({limit} вещ(и) по очереди)")
    print("=" * 70)

    if not backends.GRADIO_AVAILABLE:
        print("gradio_client не установлен: pip install gradio_client")
        return 1

    photos = srv.users_bd.get_user_photos(1)
    if not photos:
        print("В базе нет фото пользователя 1")
        return 1
    source_photo = photos[0]
    person_path = os.path.join(srv.PROJECT_ROOT, source_photo)
    print(f"\nЧеловек: {source_photo}  ({os.path.getsize(person_path)} байт)")

    products = srv.users_bd.get_product()[:limit]
    if not products:
        print("В базе нет товаров")
        return 1

    garments, err = srv._collect_products([p["id"] for p in products])
    if err:
        print(f"Ошибка сбора товаров: {err}")
        return 1

    total = len(garments)
    print(f"Одежда:  {total} шт. (надеваются по очереди)")
    for i, g in enumerate(garments, 1):
        print(f"   {i}. {g['name']} ({g['type']}) -> {os.path.basename(g['path'])}")

    # --- перебираем движки ---
    for cfg in srv.TRYON["backends"]:
        backend_id = cfg["id"]
        space = backends.BACKENDS[backend_id]["space"]
        print("\n" + "=" * 70)
        print(f"ДВИЖОК: {backend_id}   ({space})")
        print("=" * 70)

        current_person = person_path
        steps_done = []

        for index, garment in enumerate(garments, 1):
            prompt = backends.build_tryon_prompt([garment["description"]],
                                                 index=index, total=total)
            print(f"\n--- Шаг {index}/{total}: {garment['name']} ---")
            print(f"    Промпт: {prompt}")
            print(f"    Вход (человек): {os.path.basename(current_person)}")

            options = {
                "prompt": prompt,
                "steps": int(cfg.get("steps", srv.TRYON["steps"])),
                "seed": int(cfg.get("seed", srv.TRYON["seed"])),
                "timeout": cfg.get("timeout", 420),
                "connect_timeout": cfg.get("connect_timeout", srv.TRYON["connect_timeout"]),
                "token": srv.HF_TOKEN,
            }

            t0 = time.time()
            ok, path, error = backends.run_backend(
                backend_id, current_person, garment, options
            )
            elapsed = time.time() - t0

            if not ok:
                print(f"    ❌ НЕУДАЧА за {elapsed:.1f} с")
                print(f"       причина: {error}")
                break

            # сохраняем шаг рядом с другими результатами
            out_dir = os.path.join(srv.UPLOADS_DIR, "user_1")
            os.makedirs(out_dir, exist_ok=True)
            name = f"test_{backend_id}_step{index}_{uuid.uuid4().hex[:8]}.png"
            saved = os.path.join(out_dir, name)
            shutil.copy(path, saved)
            steps_done.append(saved)
            current_person = saved  # следующий шаг надеваем поверх этого

            print(f"    ✅ Шаг {index} за {elapsed:.1f} с")
            print(f"       файл:   {name}")
            print(f"       размер: {os.path.getsize(saved)} байт")

        if steps_done:
            print(f"\n✅ ДВИЖОК {backend_id}: выполнено шагов {len(steps_done)}/{total}")
            for s in steps_done:
                print(f"   {os.path.basename(s)}")
            print("\nОткройте последний файл — там человек во всей выбранной одежде.")
            return 0
        else:
            print(f"\n❌ ДВИЖОК {backend_id}: ни одного шага не выполнено")

    print("\n" + "=" * 70)
    print("Ни один движок не сработал.")
    print("Самая частая причина — исчерпана квота ZeroGPU у аккаунта Hugging Face.")
    print("Проверить: https://huggingface.co/settings/billing")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
