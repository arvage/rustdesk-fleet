// WebAuthn helpers shared by the provider console's sign-in and enrollment
// pages. beginUrl returns the server's options JSON; completeUrl gets the
// browser's answer. Both resolve to the server's JSON reply or throw.
function rdfB64urlToBuf(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}
function rdfBufToB64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
async function rdfPost(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok || data.error) throw new Error(data.error || 'Request failed.');
  return data;
}
async function rdfPasskeyGet(beginUrl, completeUrl) {
  if (!window.PublicKeyCredential) throw new Error('This browser does not support passkeys.');
  const options = await rdfPost(beginUrl);
  options.challenge = rdfB64urlToBuf(options.challenge);
  options.allowCredentials = (options.allowCredentials || []).map(c => ({ ...c, id: rdfB64urlToBuf(c.id) }));
  const cred = await navigator.credentials.get({ publicKey: options });
  return rdfPost(completeUrl, {
    id: cred.id, rawId: rdfBufToB64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: rdfBufToB64url(cred.response.clientDataJSON),
      authenticatorData: rdfBufToB64url(cred.response.authenticatorData),
      signature: rdfBufToB64url(cred.response.signature),
      userHandle: cred.response.userHandle ? rdfBufToB64url(cred.response.userHandle) : null,
    },
  });
}
async function rdfPasskeyCreate(beginUrl, completeUrl, nickname) {
  if (!window.PublicKeyCredential) throw new Error('This browser does not support passkeys.');
  const options = await rdfPost(beginUrl);
  options.challenge = rdfB64urlToBuf(options.challenge);
  options.user.id = rdfB64urlToBuf(options.user.id);
  options.excludeCredentials = (options.excludeCredentials || []).map(c => ({ ...c, id: rdfB64urlToBuf(c.id) }));
  const cred = await navigator.credentials.create({ publicKey: options });
  return rdfPost(completeUrl, {
    id: cred.id, rawId: rdfBufToB64url(cred.rawId), type: cred.type, nickname: nickname || '',
    response: {
      clientDataJSON: rdfBufToB64url(cred.response.clientDataJSON),
      attestationObject: rdfBufToB64url(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
  });
}
