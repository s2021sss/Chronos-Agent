"""
HTML шаблоны для OAuth и других страниц.
"""

OAUTH_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chronos — авторизация</title>
  <style>
    body {{
      font-family: sans-serif;
      max-width: 480px;
      margin: 80px auto;
      text-align: center;
      color: #333;
    }}
    h2 {{ color: #2e7d32; }}
  </style>
</head>
<body>
  <h2>✅ Google Calendar подключён!</h2>
  <p>Вернись в Telegram — бот пришлёт дальнейшие инструкции.</p>
  <p style="color:#999; font-size:0.9em;">Это окно можно закрыть.</p>
</body>
</html>"""

OAUTH_ERROR_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chronos — ошибка</title>
  <style>
    body {{
      font-family: sans-serif;
      max-width: 480px;
      margin: 80px auto;
      text-align: center;
      color: #333;
    }}
    h2 {{ color: #c62828; }}
    code {{
      background: #f5f5f5;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.85em;
    }}
  </style>
</head>
<body>
  <h2>❌ Ошибка авторизации</h2>
  <p><code>{reason}</code></p>
  <p>Вернись в Telegram и попробуй ещё раз (/start).</p>
</body>
</html>"""
