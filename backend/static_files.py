"""``StaticFiles`` с принудительным ``Cache-Control: no-cache``.

В этом бандле Starlette конструктор ``StaticFiles`` ещё не принимает
kwarg ``headers``, поэтому добавляем заголовок в ``file_response`` —
единственную точку, где формируется ответ для реального файла.
``NotModifiedResponse`` (304) переносит ``cache-control`` явно, так что
ETag/If-Modified-Since продолжают работать на горячих ресурсах.

Зачем ``no-cache, must-revalidate``: браузер обязан каждый раз
спрашивать сервер, есть ли новая версия. Без этого пользователь
продолжает видеть старую вёрстку после правок CSS/JS, даже когда
мы инкрементируем ``?v=N``.
"""

from __future__ import annotations

from fastapi.staticfiles import StaticFiles


class NoCacheStaticFiles(StaticFiles):
    """``StaticFiles`` + ``Cache-Control: no-cache, must-revalidate``."""

    def file_response(self, full_path, stat_result, scope, status_code=200):  # type: ignore[override]
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


__all__ = ["NoCacheStaticFiles"]
