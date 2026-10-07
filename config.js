/* =========================================================================
 * config.js — единая точка настройки адреса Flask-сервера.
 *
 * Здесь НЕТ жёстко прописанных IP. Адрес сервера вычисляется автоматически
 * из адреса страницы, поэтому проект работает на любом компьютере и в сети:
 *
 *   Live Server  → http://127.0.0.1:5500/index/index.html
 *                  запросы идут на http://127.0.0.1:5000/api/...
 *   Другой ПК    → http://192.168.1.42:5500/index/index.html
 *                  запросы идут на http://192.168.1.42:5000/api/...
 *   Файлом       → file:///.../index/index.html
 *                  запросы идут на http://localhost:5000/api/...
 *
 * Порт Flask-сервера при необходимости меняется одной строкой ниже
 * (он же должен совпадать с портом в server/server.py).
 * ========================================================================= */

(function (global) {
    'use strict';

    /** Порт, на котором запускается server/server.py */
    var SERVER_PORT = 5000;

    /** Адрес страницы */
    var protocol = global.location.protocol;   // 'http:' | 'https:' | 'file:'
    var hostname = global.location.hostname;   // 'localhost' | '127.0.0.1' | '192.168.x.x' | ''

    // Если страница открыта как файл, location.hostname пустой — берём localhost.
    var serverHost = hostname || 'localhost';

    // http для обычной работы, https если страница уже открыта по https
    // (иначе браузер заблокирует запрос как mixed content).
    var serverProtocol = protocol === 'https:' ? 'https:' : 'http:';

    /** Полный адрес Flask-сервера, например http://127.0.0.1:5000 */
    var SERVER_ORIGIN = serverProtocol + '//' + serverHost + ':' + SERVER_PORT;

    /**
     * Приводит путь из базы данных или от сервера к абсолютному URL картинки.
     *
     * Примеры:
     *   '../images/1.png'                 -> http://127.0.0.1:5000/images/1.png
     *   'uploads/user_1/gen_abc.png'      -> http://127.0.0.1:5000/uploads/user_1/gen_abc.png
     *   'https://example.com/a.png'       -> без изменений
     *   ''  /  null  /  undefined         -> fallback (если задан), иначе ''
     *
     * @param {string} path     путь к картинке
     * @param {string} fallback что вернуть, если путь пустой
     * @returns {string} абсолютный URL
     */
    function resolveAsset(path, fallback) {
        if (!path) return fallback || '';

        var value = String(path).trim();

        // Уже абсолютный адрес или data:URL — не трогаем.
        if (/^(https?:)?\/\//i.test(value) || /^data:/i.test(value)) return value;

        // Якорь (например '#') — не картинка.
        if (value.charAt(0) === '#') return fallback || '';

        // Путь относительно корня проекта: '../images/1.png', './uploads/a.png', 'images/1.png'
        var normalized = value.replace(/^(\.\.?\/)+/, '').replace(/^\/+/, '');

        return SERVER_ORIGIN + '/' + normalized;
    }

    /**
     * Собирает адрес эндпоинта API.
     *   api('/api/products')        -> http://127.0.0.1:5000/api/products
     *   api('api/products')         -> то же самое
     */
    function api(endpoint) {
        return SERVER_ORIGIN + '/' + String(endpoint).replace(/^\/+/, '');
    }

    // Если страница открыта двойным щелчком по файлу — fetch к серверу
    // заблокируется браузером. Предупреждаем понятным текстом.
    if (protocol === 'file:') {
        console.warn(
            '[config.js] Страница открыта как файл (file://).\n' +
            'Запросы к серверу будут заблокированы браузером.\n' +
            'Откройте проект через Live Server (VS Code) или через Flask-сервер.'
        );
    }

    global.MAI_CONFIG = {
        SERVER_PORT: SERVER_PORT,
        SERVER_ORIGIN: SERVER_ORIGIN,
        resolveAsset: resolveAsset,
        api: api
    };

    // Короткие псевдонимы, чтобы в коде страниц было короче.
    global.SERVER_ORIGIN = SERVER_ORIGIN;
    global.resolveAsset = resolveAsset;
    global.apiUrl = api;
})(window);
