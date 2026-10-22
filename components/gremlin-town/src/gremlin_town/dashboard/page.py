"""Empty authenticated shell. All data comes from fresh protected JSON reads."""


def page_html(*, actions=False, ask=False):
    actions_script = '<script src="/assets/actions.js" defer></script>' if actions else ''
    if ask:
        actions_script += '<script src="/assets/ask.js" defer></script>'
    return '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141311"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Exosuit"><title>My day · Exosuit</title>
<link rel="stylesheet" href="/assets/dashboard.css"><script src="/assets/dashboard.js" defer></script>''' + actions_script + '''</head>
<body><div class="wrap"><header class="top"><span class="mark" aria-hidden="true"></span><span class="date" id="date">Exosuit</span>
<nav class="switch" aria-label="View"><button id="tab-day" aria-pressed="true">My day</button><button id="tab-work" aria-pressed="false">All my work</button></nav>
<button class="back" id="refresh">Refresh</button><button class="back" id="sign-out">Sign out</button></header>
<main id="view" aria-live="polite"><p class="hint">Loading your day…</p></main><noscript>JavaScript is needed to show your dashboard.</noscript></div>
<div class="toast" id="toast" role="status"><span id="toast-text"></span></div></body></html>'''
