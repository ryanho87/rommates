#!/usr/bin/env node
"use strict";
// Wait for a ROMmates build to finish processing on App Store Connect, then:
//   --channel dev     stop (the Nightly group already has it).
//   --channel stable  add it to the Stable TestFlight group and announce it (see announce() at the bottom).
// Usage: node scripts/await-build.js <build> [--channel dev|stable] [--body ".."] [--title ".."] [--timeout-min N] [--dry]
const fs = require("fs"), path = require("path"), crypto = require("crypto"), { execFileSync } = require("child_process");
const KEY_ID = "RZ3522K2CN", ISSUER = "30990242-ebdc-49b3-9961-f9d745ce67bd";
const KEY_PATH = path.join(process.env.HOME, ".appstoreconnect/private_keys", `AuthKey_${KEY_ID}.p8`);
const BUNDLE = "com.rommates.app", STABLE_GROUP = "Stable";
const arg = (n) => { const i = process.argv.indexOf(`--${n}`); return i > -1 ? process.argv[i + 1] : null; };
const has = (n) => process.argv.includes(`--${n}`);
const build = process.argv[2];
if (!build || !/^\d+$/.test(build)) { console.error("usage: await-build.js <build> [--channel dev|stable] [--body ..] [--title ..] [--timeout-min N] [--dry]"); process.exit(2); }
const CHANNEL = arg("channel") || "stable";
if (!["dev", "stable"].includes(CHANNEL)) { console.error(`unknown channel ${CHANNEL}`); process.exit(2); }
const b64url = (b) => Buffer.from(b).toString("base64").replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_");
function token() {
  const header = b64url(JSON.stringify({ alg: "ES256", kid: KEY_ID, typ: "JWT" }));
  const now = Math.floor(Date.now() / 1000);
  const claims = b64url(JSON.stringify({ iss: ISSUER, iat: now, exp: now + 15 * 60, aud: "appstoreconnect-v1" }));
  const key = crypto.createPrivateKey(fs.readFileSync(KEY_PATH));
  const sig = crypto.sign("sha256", Buffer.from(`${header}.${claims}`), { key, dsaEncoding: "ieee-p1363" });
  return `${header}.${claims}.${b64url(sig)}`;
}
async function asc(pathname, method = "GET", body) {
  const r = await fetch(`https://api.appstoreconnect.apple.com${pathname}`, { method, headers: { authorization: `Bearer ${token()}`, ...(body ? { "content-type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`ASC ${r.status} ${method} ${pathname}: ${(await r.text()).slice(0, 200)}`);
  return r.status === 204 ? null : r.json();
}
async function state(appId) {
  const b = await asc(`/v1/builds?filter[app]=${appId}&filter[version]=${build}&sort=-uploadedDate&limit=1&include=buildBetaDetail`);
  const row = b.data[0]; if (!row) return null;
  const detail = (b.included || []).find((x) => x.type === "buildBetaDetails");
  return { processing: row.attributes.processingState, internal: detail?.attributes?.internalBuildState, external: detail?.attributes?.externalBuildState };
}
async function addToGroup(appId, groupName) {
  const g = (await asc(`/v1/betaGroups?filter[app]=${appId}&filter[name]=${encodeURIComponent(groupName)}`)).data[0];
  if (!g) throw new Error(`no TestFlight group named ${groupName}`);
  const b = (await asc(`/v1/builds?filter[app]=${appId}&filter[version]=${build}&sort=-uploadedDate&limit=1`)).data[0];
  await asc(`/v1/betaGroups/${g.id}/relationships/builds`, "POST", { data: [{ type: "builds", id: b.id }] });
  return g.attributes.name;
}
const version = () => (fs.readFileSync(path.join(__dirname, "..", "project.yml"), "utf8").match(/^\s*MARKETING_VERSION:\s*"?([0-9.]+)"?/m) || [])[1] || "";

(async () => {
  const appId = (await asc(`/v1/apps?filter[bundleId]=${BUNDLE}`)).data[0]?.id;
  if (!appId) throw new Error(`no app with bundle ${BUNDLE}`);
  const deadline = Date.now() + (Number(arg("timeout-min")) || 45) * 60 * 1000;
  for (;;) {
    const s = await state(appId).catch((e) => { console.warn(`  ${e.message}`); return null; });
    const stamp = new Date().toTimeString().slice(0, 8);
    if (s && s.processing === "VALID") { console.log(`${stamp} build ${build}: processed (internal ${s.internal || "?"})`); break; }
    if (s && /FAILED|INVALID/.test(s.processing || "")) { console.error(`${stamp} build ${build}: ${s.processing}`); process.exit(1); }
    console.log(`${stamp} build ${build}: ${s ? s.processing : "not visible yet"}`);
    if (Date.now() > deadline) { console.error("gave up waiting"); process.exit(1); }
    await new Promise((r) => setTimeout(r, 60 * 1000));
  }
  if (has("dry")) { console.log("dry run: not promoting"); process.exit(0); }
  if (CHANNEL === "dev") { console.log(`==> nightly build ${build} is on TestFlight for the Nightly group; promote with: scripts/promote.sh ${build} --body ".."`); process.exit(0); }
  console.log(`==> TestFlight: build ${build} added to ${await addToGroup(appId, STABLE_GROUP)}`);
  await announce(build, version(), arg("title"), arg("body"));
  process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });

// ROMmates announces through the server on the NUC: MobileReleaseService.publish writes the release manifest,
// drops an Inbox item, and pushes an APNs announcement. Notes come from --body, else ios/releases/<build>.md.
async function announce(build, version, title, body) {
  let notes = body;
  if (!notes) { const f = path.join(__dirname, "..", "releases", `${build}.md`); if (fs.existsSync(f)) notes = fs.readFileSync(f, "utf8").trim(); }
  if (!notes) { console.error(`no notes: pass --body or write ios/releases/${build}.md`); process.exit(1); }
  const py = `import json,sys; from app.main import mobile_releases; print(json.dumps(mobile_releases.publish(${Number(build)}, ${JSON.stringify(version)}, json.loads(sys.stdin.read()))))`;
  const out = execFileSync("ssh", ["-o", "BatchMode=yes", "nuc", `docker exec -i rommates python -c ${JSON.stringify(py)}`], { input: JSON.stringify(notes), encoding: "utf8" });
  console.log(`==> announced on the server: ${out.trim().split("\n").pop()}`);
}
