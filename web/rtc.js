// WHIP / WHEP yardimcilari (mediamtx /m/ altinda)
function q(name){return new URLSearchParams(location.search).get(name)||"";}

function waitIce(pc, ms){
  return new Promise(res=>{
    if(pc.iceGatheringState==="complete") return res();
    const t=setTimeout(res, ms);
    pc.addEventListener("icegatheringstatechange",()=>{
      if(pc.iceGatheringState==="complete"){clearTimeout(t);res();}
    });
  });
}

function fixLoc(loc, base){
  if(!loc) return null;
  if(loc.startsWith("/") && !loc.startsWith("/m/")) loc="/m"+loc;
  return new URL(loc, location.origin + base).toString();
}

async function sdpExchange(pc, url, headers){
  const offer=await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitIce(pc, 1500);
  const r=await fetch(url,{method:"POST",headers:Object.assign({"Content-Type":"application/sdp"},headers||{}),body:pc.localDescription.sdp});
  if(!r.ok){const e=new Error("HTTP "+r.status);e.status=r.status;throw e;}
  const answer=await r.text();
  await pc.setRemoteDescription({type:"answer",sdp:answer});
  return fixLoc(r.headers.get("Location"), url);
}

async function durum(oda, t, k){
  const r=await fetch("/api/durum?oda="+encodeURIComponent(oda)+(t?"&t="+encodeURIComponent(t):"")+(k?"&k="+encodeURIComponent(k):""),{cache:"no-store"});
  try{return await r.json();}catch(_){return {var:false};}
}
