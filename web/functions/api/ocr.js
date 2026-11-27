// Cloudflare Pages Function — optional local OCR proxy for web/index.html.
//
// The browser cannot always call an OCR endpoint directly (CORS), so this
// function relays { base_url, api_key, payload } and forwards the bearer token.
// That makes it a credentialed relay: deploy it as a *local demo helper* only.
// A production deployment MUST set ALLOWED_OCR_HOSTS and ALLOWED_ORIGINS (or
// ALLOWED_ORIGIN); with the defaults only the local Ollama endpoint is reachable.

const DEFAULT_ALLOWED_HOSTS = '127.0.0.1:11434,localhost:11434';
const MAX_BODY_BYTES = 10 * 1024 * 1024;

function parseList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
}

function allowedHosts(env) {
  const configured = parseList(env && env.ALLOWED_OCR_HOSTS);
  return configured.length > 0 ? configured : parseList(DEFAULT_ALLOWED_HOSTS);
}

// Either echo ALLOWED_ORIGIN, or reflect the request Origin when it is
// explicitly listed in ALLOWED_ORIGINS. Never fall back to '*'.
function allowedOrigin(env, request) {
  const pinned = ((env && env.ALLOWED_ORIGIN) || '').trim();
  if (pinned) return pinned;
  const origin = (request.headers.get('Origin') || '').trim();
  if (!origin) return '';
  const reflectable = parseList(env && env.ALLOWED_ORIGINS);
  return reflectable.includes(origin.toLowerCase()) ? origin : '';
}

function corsHeaders(env, request, contentType) {
  const headers = {
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    Vary: 'Origin',
  };
  const origin = allowedOrigin(env, request);
  if (origin) headers['Access-Control-Allow-Origin'] = origin;
  if (contentType) headers['Content-Type'] = contentType;
  return headers;
}

function errorResponse(env, request, status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: corsHeaders(env, request, 'application/json'),
  });
}

function byteLength(text) {
  return new TextEncoder().encode(text).byteLength;
}

export async function onRequestPost(context) {
  const { request, env } = context;

  try {
    // Reject oversized bodies before touching the payload. When the caller
    // omits content-length the body has to be read once, so measure the text
    // we already consumed instead of reading it a second time.
    const contentLength = request.headers.get('content-length');
    if (contentLength !== null && Number(contentLength) > MAX_BODY_BYTES) {
      return errorResponse(env, request, 413, 'Request body too large');
    }

    const raw = await request.text();
    if (byteLength(raw) > MAX_BODY_BYTES) {
      return errorResponse(env, request, 413, 'Request body too large');
    }

    let body;
    try {
      body = JSON.parse(raw);
    } catch {
      return errorResponse(env, request, 400, 'Invalid JSON body');
    }

    const { base_url, api_key, payload } = body || {};

    if (!base_url || !api_key || !payload) {
      return errorResponse(env, request, 400, 'Missing base_url, api_key, or payload');
    }

    let parsed;
    try {
      parsed = new URL(String(base_url));
    } catch {
      return errorResponse(env, request, 400, 'Invalid base_url');
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return errorResponse(env, request, 400, 'base_url must use http or https');
    }
    if (parsed.username || parsed.password) {
      return errorResponse(env, request, 400, 'Credentials in base_url are not allowed');
    }
    if (parsed.search || parsed.hash) {
      return errorResponse(env, request, 400, 'Query string or fragment in base_url is not allowed');
    }
    if (parsed.pathname.split('/').some((seg) => seg === '..')) {
      return errorResponse(env, request, 400, 'Invalid path in base_url');
    }
    if (!allowedHosts(env).includes(parsed.host.toLowerCase())) {
      return errorResponse(env, request, 403, 'base_url host is not allowed');
    }

    const cleanPath = parsed.pathname.replace(/\/+$/, '');
    const endpoint = `${parsed.origin}${cleanPath}/ocr`;
    const resp = await fetch(endpoint, {
      method: 'POST',
      redirect: 'error',
      headers: {
        'Authorization': `Bearer ${api_key}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    const data = await resp.text();
    return new Response(data, {
      status: resp.status,
      headers: corsHeaders(env, request, resp.headers.get('content-type') || 'application/json'),
    });
  } catch (e) {
    return errorResponse(env, request, 502, e.message);
  }
}

// Handle CORS preflight
export async function onRequestOptions(context) {
  const { request, env } = context;
  return new Response(null, { status: 204, headers: corsHeaders(env, request) });
}
