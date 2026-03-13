# Public Hub edge 502 while origin is healthy

- captured_at_utc: 2026-03-09T07:28:11Z
- public_url: https://admin.slate.ceo/oc/brain/hub/analytics
- public_status: 502
- local_url: http://127.0.0.1/hub/analytics
- local_status: 200

## Interpretation
Repeated evidence still points to an edge/proxy failure in front of origin request handling, not a local Hub process failure.

## Public response headers
```
HTTP/2 502 
date: Mon, 09 Mar 2026 07:28:11 GMT
content-type: text/plain; charset=UTF-8
content-length: 15
cache-control: private, max-age=0, no-store, no-cache, must-revalidate, post-check=0, pre-check=0
expires: Thu, 01 Jan 1970 00:00:01 GMT
referrer-policy: same-origin
x-frame-options: SAMEORIGIN
server: cloudflare
cf-ray: 9d985c851c953418-EWR
alt-svc: h3=":443"; ma=86400

```

## Local response headers
```
HTTP/1.1 200 OK
Server: nginx/1.22.1
Date: Mon, 09 Mar 2026 07:28:11 GMT
Content-Type: application/json
Content-Length: 21657
Connection: keep-alive

```
