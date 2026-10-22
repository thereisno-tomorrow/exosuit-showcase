"use strict";
// The enrolment token is a fragment: it never enters an HTTP URL or referrer.
// Remove it from this history entry immediately; keep it only in this closure.
(() => {
  let token = location.pathname === "/enrol" ? new URLSearchParams(location.hash.slice(1)).get("token") : null;
  if (location.hash) history.replaceState(null, "", location.pathname);
  const button = document.getElementById("sign-in");
  const status = document.getElementById("status");
  const enrolling = location.pathname === "/enrol";
  if (enrolling) {
    document.getElementById("intro").textContent = "Create a passkey to open your day with Face ID or your device's sign-in.";
    button.textContent = "Create passkey";
    if (!token) { status.textContent = "Open your one-time enrolment link again."; button.disabled = true; }
  }
  const decode = value => Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), char => char.charCodeAt(0));
  const encode = value => btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  async function post(path, body, csrf) {
    const result = await fetch(path, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf}, body:JSON.stringify(body)});
    if (!result.ok) throw new Error("Sign-in failed");
    return result.json();
  }
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.textContent = "";
    try {
      if (!window.PublicKeyCredential) throw new Error("Passkeys unavailable");
      const context = await fetch("/auth/context", {credentials:"same-origin"});
      if (!context.ok) throw new Error("Sign-in unavailable");
      const {csrf} = await context.json();
      const kind = enrolling ? "enrol" : "sign-in";
      const options = await post(`/auth/${kind}/options`, enrolling ? {token} : {}, csrf);
      const publicKey = options.publicKey;
      publicKey.challenge = decode(publicKey.challenge);
      if (enrolling) {
        publicKey.user.id = decode(publicKey.user.id);
        publicKey.excludeCredentials = (publicKey.excludeCredentials || []).map(c => ({...c, id:decode(c.id)}));
      } else {
        publicKey.allowCredentials = (publicKey.allowCredentials || []).map(c => ({...c, id:decode(c.id)}));
      }
      const credential = enrolling ? await navigator.credentials.create({publicKey}) : await navigator.credentials.get({publicKey});
      const response = {clientDataJSON:encode(credential.response.clientDataJSON)};
      if (enrolling) {
        response.attestationObject = encode(credential.response.attestationObject);
        if (credential.response.getTransports) response.transports = credential.response.getTransports();
      } else {
        response.authenticatorData = encode(credential.response.authenticatorData);
        response.signature = encode(credential.response.signature);
        response.userHandle = credential.response.userHandle ? encode(credential.response.userHandle) : null;
      }
      await post(`/auth/${kind}/verify`, {challenge_id:options.challenge_id, credential:{id:credential.id, rawId:encode(credential.rawId), type:credential.type, response}}, csrf);
      token = null;
      location.replace("/");
    } catch (_) {
      status.textContent = enrolling ? "Couldn't create the passkey. Try again, or request a new enrolment link." : "Couldn't sign in. Please try again.";
      button.disabled = false;
    }
  });
})();
