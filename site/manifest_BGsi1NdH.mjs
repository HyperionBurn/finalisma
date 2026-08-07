import '@astrojs/internal-helpers/path';
import 'kleur/colors';
import { N as NOOP_MIDDLEWARE_HEADER, h as decodeKey } from './chunks/astro/server_DPIGCsi0.mjs';
import 'clsx';
import 'es-module-lexer';
import 'html-escaper';

const NOOP_MIDDLEWARE_FN = async (_ctx, next) => {
  const response = await next();
  response.headers.set(NOOP_MIDDLEWARE_HEADER, "true");
  return response;
};

const codeToStatusMap = {
  // Implemented from IANA HTTP Status Code Registry
  // https://www.iana.org/assignments/http-status-codes/http-status-codes.xhtml
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  PAYMENT_REQUIRED: 402,
  FORBIDDEN: 403,
  NOT_FOUND: 404,
  METHOD_NOT_ALLOWED: 405,
  NOT_ACCEPTABLE: 406,
  PROXY_AUTHENTICATION_REQUIRED: 407,
  REQUEST_TIMEOUT: 408,
  CONFLICT: 409,
  GONE: 410,
  LENGTH_REQUIRED: 411,
  PRECONDITION_FAILED: 412,
  CONTENT_TOO_LARGE: 413,
  URI_TOO_LONG: 414,
  UNSUPPORTED_MEDIA_TYPE: 415,
  RANGE_NOT_SATISFIABLE: 416,
  EXPECTATION_FAILED: 417,
  MISDIRECTED_REQUEST: 421,
  UNPROCESSABLE_CONTENT: 422,
  LOCKED: 423,
  FAILED_DEPENDENCY: 424,
  TOO_EARLY: 425,
  UPGRADE_REQUIRED: 426,
  PRECONDITION_REQUIRED: 428,
  TOO_MANY_REQUESTS: 429,
  REQUEST_HEADER_FIELDS_TOO_LARGE: 431,
  UNAVAILABLE_FOR_LEGAL_REASONS: 451,
  INTERNAL_SERVER_ERROR: 500,
  NOT_IMPLEMENTED: 501,
  BAD_GATEWAY: 502,
  SERVICE_UNAVAILABLE: 503,
  GATEWAY_TIMEOUT: 504,
  HTTP_VERSION_NOT_SUPPORTED: 505,
  VARIANT_ALSO_NEGOTIATES: 506,
  INSUFFICIENT_STORAGE: 507,
  LOOP_DETECTED: 508,
  NETWORK_AUTHENTICATION_REQUIRED: 511
};
Object.entries(codeToStatusMap).reduce(
  // reverse the key-value pairs
  (acc, [key, value]) => ({ ...acc, [value]: key }),
  {}
);

function sanitizeParams(params) {
  return Object.fromEntries(
    Object.entries(params).map(([key, value]) => {
      if (typeof value === "string") {
        return [key, value.normalize().replace(/#/g, "%23").replace(/\?/g, "%3F")];
      }
      return [key, value];
    })
  );
}
function getParameter(part, params) {
  if (part.spread) {
    return params[part.content.slice(3)] || "";
  }
  if (part.dynamic) {
    if (!params[part.content]) {
      throw new TypeError(`Missing parameter: ${part.content}`);
    }
    return params[part.content];
  }
  return part.content.normalize().replace(/\?/g, "%3F").replace(/#/g, "%23").replace(/%5B/g, "[").replace(/%5D/g, "]");
}
function getSegment(segment, params) {
  const segmentPath = segment.map((part) => getParameter(part, params)).join("");
  return segmentPath ? "/" + segmentPath : "";
}
function getRouteGenerator(segments, addTrailingSlash) {
  return (params) => {
    const sanitizedParams = sanitizeParams(params);
    let trailing = "";
    if (addTrailingSlash === "always" && segments.length) {
      trailing = "/";
    }
    const path = segments.map((segment) => getSegment(segment, sanitizedParams)).join("") + trailing;
    return path || "/";
  };
}

function deserializeRouteData(rawRouteData) {
  return {
    route: rawRouteData.route,
    type: rawRouteData.type,
    pattern: new RegExp(rawRouteData.pattern),
    params: rawRouteData.params,
    component: rawRouteData.component,
    generate: getRouteGenerator(rawRouteData.segments, rawRouteData._meta.trailingSlash),
    pathname: rawRouteData.pathname || void 0,
    segments: rawRouteData.segments,
    prerender: rawRouteData.prerender,
    redirect: rawRouteData.redirect,
    redirectRoute: rawRouteData.redirectRoute ? deserializeRouteData(rawRouteData.redirectRoute) : void 0,
    fallbackRoutes: rawRouteData.fallbackRoutes.map((fallback) => {
      return deserializeRouteData(fallback);
    }),
    isIndex: rawRouteData.isIndex,
    origin: rawRouteData.origin
  };
}

function deserializeManifest(serializedManifest) {
  const routes = [];
  for (const serializedRoute of serializedManifest.routes) {
    routes.push({
      ...serializedRoute,
      routeData: deserializeRouteData(serializedRoute.routeData)
    });
    const route = serializedRoute;
    route.routeData = deserializeRouteData(serializedRoute.routeData);
  }
  const assets = new Set(serializedManifest.assets);
  const componentMetadata = new Map(serializedManifest.componentMetadata);
  const inlinedScripts = new Map(serializedManifest.inlinedScripts);
  const clientDirectives = new Map(serializedManifest.clientDirectives);
  const serverIslandNameMap = new Map(serializedManifest.serverIslandNameMap);
  const key = decodeKey(serializedManifest.key);
  return {
    // in case user middleware exists, this no-op middleware will be reassigned (see plugin-ssr.ts)
    middleware() {
      return { onRequest: NOOP_MIDDLEWARE_FN };
    },
    ...serializedManifest,
    assets,
    componentMetadata,
    inlinedScripts,
    clientDirectives,
    routes,
    serverIslandNameMap,
    key
  };
}

const manifest = deserializeManifest({"hrefRoot":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/","cacheDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/node_modules/.astro/","outDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/site/","srcDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/","publicDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/public/","buildClientDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/site/client/","buildServerDir":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/site/server/","adapterName":"","routes":[{"file":"file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/site/index.html","links":[],"scripts":[],"styles":[],"routeData":{"route":"/","isIndex":true,"type":"page","pattern":"^\\/$","segments":[],"params":[],"component":"src/pages/index.astro","pathname":"/","prerender":true,"fallbackRoutes":[],"distURL":[],"origin":"project","_meta":{"trailingSlash":"never"}}}],"base":"/","trailingSlash":"never","compressHTML":true,"componentMetadata":[["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/pages/index.astro",{"propagation":"none","containsHead":true}]],"renderers":[],"clientDirectives":[["idle","(()=>{var l=(n,t)=>{let i=async()=>{await(await n())()},e=typeof t.value==\"object\"?t.value:void 0,s={timeout:e==null?void 0:e.timeout};\"requestIdleCallback\"in window?window.requestIdleCallback(i,s):setTimeout(i,s.timeout||200)};(self.Astro||(self.Astro={})).idle=l;window.dispatchEvent(new Event(\"astro:idle\"));})();"],["load","(()=>{var e=async t=>{await(await t())()};(self.Astro||(self.Astro={})).load=e;window.dispatchEvent(new Event(\"astro:load\"));})();"],["media","(()=>{var n=(a,t)=>{let i=async()=>{await(await a())()};if(t.value){let e=matchMedia(t.value);e.matches?i():e.addEventListener(\"change\",i,{once:!0})}};(self.Astro||(self.Astro={})).media=n;window.dispatchEvent(new Event(\"astro:media\"));})();"],["only","(()=>{var e=async t=>{await(await t())()};(self.Astro||(self.Astro={})).only=e;window.dispatchEvent(new Event(\"astro:only\"));})();"],["visible","(()=>{var a=(s,i,o)=>{let r=async()=>{await(await s())()},t=typeof i.value==\"object\"?i.value:void 0,c={rootMargin:t==null?void 0:t.rootMargin},n=new IntersectionObserver(e=>{for(let l of e)if(l.isIntersecting){n.disconnect(),r();break}},c);for(let e of o.children)n.observe(e)};(self.Astro||(self.Astro={})).visible=a;window.dispatchEvent(new Event(\"astro:visible\"));})();"]],"entryModules":{"\u0000@astro-page:src/pages/index@_@astro":"pages/index.astro.mjs","\u0000@astro-renderers":"renderers.mjs","\u0000noop-actions":"_noop-actions.mjs","\u0000noop-middleware":"_noop-middleware.mjs","\u0000@astrojs-manifest":"manifest_BGsi1NdH.mjs","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/ScrollController.tsx":"_astro/ScrollController.His_fbsV.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/scene/SceneCanvas.tsx":"_astro/SceneCanvas.u8k8GkP2.js","@astrojs/react/client.js":"_astro/client.COhkZpLX.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/ConnectTiers.astro?astro&type=script&index=0&lang.ts":"_astro/ConnectTiers.astro_astro_type_script_index_0_lang.DDiWNivR.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/Header.astro?astro&type=script&index=0&lang.ts":"_astro/Header.astro_astro_type_script_index_0_lang.D1bCS2Nj.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/HeroWebGL.astro?astro&type=script&index=0&lang.ts":"_astro/HeroWebGL.astro_astro_type_script_index_0_lang.CsscueNY.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/LiveDemo.astro?astro&type=script&index=0&lang.ts":"_astro/LiveDemo.astro_astro_type_script_index_0_lang.Dm88cSG1.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/Pricing.astro?astro&type=script&index=0&lang.ts":"_astro/Pricing.astro_astro_type_script_index_0_lang.DmiARdwh.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/layouts/RootLayout.astro?astro&type=script&index=0&lang.ts":"_astro/RootLayout.astro_astro_type_script_index_0_lang.D4XfaV74.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/pages/index.astro?astro&type=script&index=0&lang.ts":"_astro/index.astro_astro_type_script_index_0_lang.Digp_Rl8.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/node_modules/lenis/dist/lenis.mjs":"_astro/lenis.BBml_0t9.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/node_modules/gsap/index.js":"_astro/index.Bvu9zNsI.js","C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/node_modules/gsap/ScrollTrigger.js":"_astro/ScrollTrigger.7Zy99s9Q.js","astro:scripts/before-hydration.js":""},"inlinedScripts":[["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/ConnectTiers.astro?astro&type=script&index=0&lang.ts","(function(){document.querySelector(\"[data-tier-tabs]\");const a=document.querySelectorAll(\"[data-tier-tab]\"),u=document.querySelectorAll(\"[data-tier-panel]\"),d=document.querySelector(\"[data-tier-copy-status]\");function s(e){const n=e.getAttribute(\"data-tier-tab\");a.forEach(t=>{const o=t===e;t.setAttribute(\"aria-selected\",o?\"true\":\"false\")}),u.forEach(t=>{t.getAttribute(\"data-tier-panel\")===n?t.removeAttribute(\"hidden\"):t.setAttribute(\"hidden\",\"true\")})}a.forEach((e,n)=>{e.addEventListener(\"click\",()=>s(e)),e.addEventListener(\"keydown\",t=>{let o=n;if(t.key===\"ArrowRight\")o=(n+1)%a.length;else if(t.key===\"ArrowLeft\")o=(n-1+a.length)%a.length;else return;t.preventDefault(),a[o].focus(),s(a[o])})}),document.querySelectorAll(\".tier-panels [data-copy]\").forEach(e=>{e.addEventListener(\"click\",async()=>{const n=e.getAttribute(\"data-copy\"),r=document.querySelector(`[data-tier-panel=\"${n}\"]`)?.querySelector(\"[data-code-block]\")?.getAttribute(\"data-full\")||\"\";try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(r);else{const c=document.createElement(\"textarea\");c.value=r,c.style.position=\"fixed\",c.style.opacity=\"0\",document.body.appendChild(c),c.select(),document.execCommand(\"copy\"),document.body.removeChild(c)}d&&(d.textContent=\"Copied to clipboard.\"),e.classList.add(\"is-copied\"),setTimeout(()=>e.classList.remove(\"is-copied\"),1500)}catch{d&&(d.textContent=\"Copy failed — select and copy manually.\")}})});const f=window.matchMedia&&window.matchMedia(\"(prefers-reduced-motion: reduce)\").matches;document.querySelectorAll(\"[data-code-block]\").forEach(e=>{const n=e.getAttribute(\"data-full\")||\"\",t=e.querySelector(\"code\");if(f||!(\"IntersectionObserver\"in window)||!t){e.classList.add(\"is-done\");return}t.textContent=\"\",e.classList.add(\"is-typing\");const o=n.split(`\n`);let i=0;function r(){if(i>=o.length){e.classList.remove(\"is-typing\"),e.classList.add(\"is-done\");return}i>0&&t.appendChild(document.createTextNode(`\n`)),t.appendChild(document.createTextNode(o[i])),i+=1,window.setTimeout(r,140)}new IntersectionObserver((p,m)=>{for(const l of p)l.isIntersecting&&(m.unobserve(l.target),window.setTimeout(r,180))},{threshold:.25}).observe(e)})})();"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/Header.astro?astro&type=script&index=0&lang.ts","(function(){const n=document.querySelector(\"[data-header]\"),t=document.querySelector(\".nav-toggle\"),e=document.getElementById(\"mobile-nav\"),r=document.querySelector(\".mobile-close\");function d(){!e||!t||(e.classList.add(\"is-open\"),e.setAttribute(\"aria-hidden\",\"false\"),e.inert=!1,t.classList.add(\"is-open\"),t.setAttribute(\"aria-expanded\",\"true\"),document.body.style.overflow=\"hidden\")}function s(){!e||!t||(e.classList.remove(\"is-open\"),e.setAttribute(\"aria-hidden\",\"true\"),e.inert=!0,t.classList.remove(\"is-open\"),t.setAttribute(\"aria-expanded\",\"false\"),document.body.style.overflow=\"\")}t?.addEventListener(\"click\",()=>{t.getAttribute(\"aria-expanded\")===\"true\"?s():d()}),r?.addEventListener(\"click\",s),e?.querySelectorAll(\"a\").forEach(i=>{i.addEventListener(\"click\",s)}),document.addEventListener(\"keydown\",i=>{i.key===\"Escape\"&&s()});let o=!1;function a(){n&&(window.scrollY>20?n.classList.add(\"is-scrolled\"):n.classList.remove(\"is-scrolled\")),o=!1}window.addEventListener(\"scroll\",()=>{o||(requestAnimationFrame(a),o=!0)},{passive:!0}),a()})();"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/HeroWebGL.astro?astro&type=script&index=0&lang.ts","(function(){const n=document.querySelector(\"[data-agent-events]\"),i=document.querySelector(\"[data-agent-events-fallback]\"),a=document.querySelector(\"[data-agent-readout]\");let d=6;function l(s,e,c,o){if(!n)return;const t=document.createElement(\"li\");if(t.className=\"evt evt--\"+s,t.setAttribute(\"data-evt-kind\",s),t.setAttribute(\"data-evt-seq\",String(o||d)),t.innerHTML='<span class=\"evt-seq\">'+String(o||d).padStart(3,\"0\")+'</span><span class=\"evt-agent\">'+e+'</span><span class=\"evt-msg\">'+c+\"</span>\",n.appendChild(t),requestAnimationFrame(()=>t.classList.add(\"in\")),i){const r=t.cloneNode(!0);r.classList.add(\"in\"),i.appendChild(r)}for(;n.children.length>12;)n.removeChild(n.firstChild)}window.addEventListener(\"agent-event\",function(s){const e=s.detail;e&&(e.kind===\"refuse\"?(l(\"refuse\",e.agent,e.msg,e.seq),a&&(a.textContent=\"REFUSED: stale fencing token\")):e.kind===\"beat\"?a&&(a.textContent=e.msg||\"Simulated demo · no credentials · no live session\"):l(e.kind,e.agent,e.msg,e.seq))})})();"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/LiveDemo.astro?astro&type=script&index=0&lang.ts","(function(){const r=document.querySelector('[data-copy=\"link\"]'),p=document.querySelector(\"[data-generated-link]\"),u=document.querySelector(\"[data-copy-status]\"),i=document.querySelector(\"[data-gate-state]\"),l=document.querySelector(\"[data-gate]\"),y=document.querySelector(\"[data-trigger-refusal]\");function c(e){u&&(u.textContent=e)}async function g(e){try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(e),c(\"Copied to clipboard.\");return}}catch{}try{const n=document.createElement(\"textarea\");n.value=e,n.style.position=\"fixed\",n.style.opacity=\"0\",document.body.appendChild(n),n.select(),document.execCommand(\"copy\"),document.body.removeChild(n),c(\"Copied to clipboard.\")}catch{c(\"Copy failed — select and copy manually.\")}}r?.addEventListener(\"click\",()=>{const e=p?.textContent?.trim()||\"\";g(e),r.classList.add(\"is-copied\"),setTimeout(()=>r.classList.remove(\"is-copied\"),1500)});function w(){i&&(i.textContent=\"REFUSED: stale fencing token\",i.setAttribute(\"data-state\",\"refuse\")),l&&(l.classList.add(\"is-refusing\"),setTimeout(()=>l.classList.remove(\"is-refusing\"),600)),c(\"Gate refused: stale fencing token.\"),setTimeout(()=>{i&&(i.textContent=\"armed\",i.removeAttribute(\"data-state\"))},3e3)}y?.addEventListener(\"click\",()=>{window.WeftScene?.fireGateRefusal?window.WeftScene.fireGateRefusal():w()});const t=document.querySelector(\"[data-terminal]\");if(t){let e=function(){if(d>=f.length){t.classList.remove(\"is-typing\"),t.classList.add(\"is-done\");return}const s=f[d];if(d>0&&o.appendChild(document.createTextNode(`\n`)),s.includes(\"<span\")){const a=document.createElement(\"span\");for(a.innerHTML=s;a.firstChild;)o.appendChild(a.firstChild)}else o.appendChild(document.createTextNode(s));d+=1,window.setTimeout(e,160)};const n=t.getAttribute(\"data-full\")||\"\";if(window.matchMedia&&window.matchMedia(\"(prefers-reduced-motion: reduce)\").matches||!(\"IntersectionObserver\"in window)){t.classList.add(\"is-done\");return}const o=t.querySelector(\"code\");o&&(o.textContent=\"\"),t.classList.add(\"is-typing\");const f=n.split(`\n`);let d=0;new IntersectionObserver((s,a)=>{for(const m of s)m.isIntersecting&&(a.unobserve(m.target),window.setTimeout(e,200))},{threshold:.25}).observe(t)}})();"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/components/Pricing.astro?astro&type=script&index=0&lang.ts","(function(){const n=document.getElementById(\"cohort-form\"),i=document.querySelector(\"[data-cohort-status]\");function a(o){i&&(i.textContent=o)}n?.addEventListener(\"submit\",async o=>{o.preventDefault();const e=new FormData(n),c=(e.get(\"team\")||\"\").trim(),r=(e.get(\"contact\")||\"\").trim(),s=(e.get(\"hosts\")||\"\").trim(),l=(e.get(\"scenario\")||\"\").trim();if(!c||!r){a(\"Team and contact email are required.\");return}const d=[\"=== Weft Managed Pilot Application ===\",\"\",`Team: ${c}`,`Contact: ${r}`,s?`Hosts: ${s}`:null,l?`Scenario: ${l}`:null,\"\",\"Generated: \"+new Date().toISOString(),\"Source: Weft marketing site (clipboard copy — nothing transmitted).\"].filter(Boolean).join(`\n`);try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(d);else{const t=document.createElement(\"textarea\");t.value=d,t.style.position=\"fixed\",t.style.opacity=\"0\",document.body.appendChild(t),t.select(),document.execCommand(\"copy\"),document.body.removeChild(t)}a(\"Application copied to clipboard. Paste it into an email to apply.\")}catch{a(\"Copy failed — select and copy the text manually.\")}})})();"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/layouts/RootLayout.astro?astro&type=script&index=0&lang.ts","document.documentElement.classList.add(\"js\");"],["C:/Users/Wasif/Documents/Multiplayer-AI-isolated/web/src/pages/index.astro?astro&type=script&index=0&lang.ts","(function(){if(typeof window>\"u\"||!(\"IntersectionObserver\"in window)){document.querySelectorAll(\".scroll-in\").forEach(function(e){e.classList.add(\"in\")});return}var i=new IntersectionObserver(function(e){e.forEach(function(o){if(o.isIntersecting){var t=o.target,n=0,a=getComputedStyle(t),s=a.getPropertyValue(\"--delay\").trim();s&&(n=parseInt(s,10),isNaN(n)&&(n=0)),n>0?setTimeout(function(){t.classList.add(\"in\")},n):t.classList.add(\"in\"),i.unobserve(t)}})},{threshold:.2});function r(){document.querySelectorAll(\".scroll-in\").forEach(function(e){i.observe(e)})}document.readyState===\"loading\"?document.addEventListener(\"DOMContentLoaded\",r):r()})();"]],"assets":["/file:///C:/Users/Wasif/Documents/Multiplayer-AI-isolated/site/index.html"],"buildFormat":"directory","checkOrigin":false,"serverIslandNameMap":[],"key":"Sk5s0i1UkIu6SFuOEQnBDRTmh5Me/YiQvezZELaQE6g="});
if (manifest.sessionConfig) manifest.sessionConfig.driverModule = null;

export { manifest };
