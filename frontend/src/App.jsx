import { useState, useEffect, useRef, useCallback } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from 'recharts'
import './index.css'

const API = 'http://localhost:8000'
const WS  = 'ws://localhost:8000'

// ── helpers ───────────────────────────────────────────
const ACTION_CHIP = {
  REQUEST:'chip-red', ACCEPT:'chip-green', LOCK:'chip-green', DECLINE:'chip-red',
  WITHDRAW:'chip-red', OFFER:'chip-blue', CONDITIONAL_OFFER:'chip-blue',
  COUNTER:'chip-amber', REQUEST_HUMAN_CONFIRM:'chip-purple', ESCALATE:'chip-amber',
  PROPOSE_ALTERNATIVE:'chip-amber', RELEASE:'chip-gray',
}
const avatarType = f => !f ? 'human'
  : f.startsWith('guardian') ? 'guardian'
  : f.startsWith('proxy') ? 'proxy'
  : f.startsWith('exchange') ? 'exchange' : 'human'
const avatarLabel = f => !f ? 'H' : f[0].toUpperCase()
const timeStr = ts => { try { return new Date(ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) } catch { return '' } }
const fmt = n => (n==null?'—':Number(n).toLocaleString())
const parseMeta = m => { if(!m) return null; if(typeof m==='object') return m; try { return JSON.parse(m) } catch { return null } }

// ── circular gauge ────────────────────────────────────
function Gauge({ value, max, label }) {
  const pct = max>0 ? Math.min(1, value/max) : 0
  const r = 54, c = 2*Math.PI*r
  const color = pct>=1 ? 'var(--green-400)' : pct>0 ? 'var(--amber-400)' : 'var(--red-400)'
  return (
    <div className="gauge-ring">
      <svg width="130" height="130">
        <circle cx="65" cy="65" r={r} fill="none" stroke="var(--gauge-track)" strokeWidth="11"/>
        <circle cx="65" cy="65" r={r} fill="none" stroke={color} strokeWidth="11" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c*(1-pct)} style={{transition:'stroke-dashoffset .6s ease, stroke .3s'}}/>
      </svg>
      <div className="gauge-center">
        <div className="big" style={{color}}>{value}<span style={{color:'var(--text-muted)',fontSize:18}}>/{max}</span></div>
        <div className="lbl">{label}</div>
      </div>
    </div>
  )
}

// ══════════════════════════════════════════════════════
// NEGOTIATION CONSOLE
// ══════════════════════════════════════════════════════
function Console({ bridges, stats, refresh }) {
  const [messages, setMessages] = useState([])
  const [negId, setNegId] = useState(null)
  const [status, setStatus] = useState('idle')
  const [coverage, setCoverage] = useState({current:0, needed:1})
  const [round, setRound] = useState(0)
  const [sel, setSel] = useState(0)
  const [typing, setTyping] = useState(false)
  const [acted, setActed] = useState({})   // uid -> 'confirmed' | 'declined'
  const [confirmedDonors, setConfirmedDonors] = useState([])
  const [mode, setMode] = useState('request')   // 'request' (patient intake) | 'bridge'
  const [hospitals, setHospitals] = useState([])
  const [intake, setIntake] = useState({blood_group:'B+', hospital_id:'', units:1, days:3, time:'9:00 AM', emergency:true, radius_km:10})
  const [reqInfo, setReqInfo] = useState(null)
  const ws = useRef(null)
  const end = useRef(null)
  const negRef = useRef(null)

  useEffect(()=>{ fetch(`${API}/hospitals`).then(r=>r.json()).then(d=>{setHospitals(d.hospitals||[]); if(d.hospitals?.[0]) setIntake(i=>({...i, hospital_id:d.hospitals[0].id}))}).catch(()=>{}) },[])

  useEffect(()=>{ end.current?.scrollIntoView({behavior:'smooth'}) }, [messages, typing])

  const addMsg = useCallback(m=>{
    setTyping(true)
    setTimeout(()=>{
      setTyping(false)
      setMessages(p=>[...p, {...m, _id: `${m.ts||''}${Math.random()}`}])
      if (m.action==='LOCK') setCoverage(p=>({...p, current:p.current+1}))
      if (m.round!==undefined) setRound(r=>Math.max(r, m.round))
      const meta = parseMeta(m.meta)
      if (meta?.profile) setConfirmedDonors(p => p.some(x=>x.user_id===meta.profile.user_id) ? p : [...p, meta.profile])
    }, 300 + Math.random()*450)
  },[])

  const connect = useCallback(id=>{
    ws.current?.close()
    const s = new WebSocket(`${WS}/ws/negotiation/${id}`)
    s.onmessage = e=>{
      try {
        const d = JSON.parse(e.data)
        if (d.type==='negotiation_complete'){ setStatus('done'); refresh?.(); return }
        // Skip internal control broadcasts (human_confirmed/declined, etc.) — only
        // render real protocol messages that have an action or a spoken line.
        if (d.type || (!d.action && !d.say)) return
        addMsg(d)
      } catch {}
    }
    s.onclose = ()=> setStatus(x=> x==='running'?'done':x)
    ws.current = s
  },[addMsg, refresh])

  const resetRun = ()=>{ setMessages([]); setRound(0); setStatus('running'); setActed({}); setConfirmedDonors([]) }

  const submitIntake = async()=>{
    resetRun(); setReqInfo(null)
    setCoverage({current:0, needed: intake.units||1})
    try {
      const r = await fetch(`${API}/intake`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(intake)}).then(async r=>{ if(!r.ok) throw new Error((await r.json()).detail||'error'); return r.json() })
      setNegId(r.neg_id); negRef.current = r.neg_id
      setReqInfo(r); setCoverage(p=>({...p, needed:r.units||1}))
      connect(r.neg_id)
    } catch(e){ setStatus('idle'); alert('Request failed: '+e.message) }
  }

  const trigger = async()=>{
    resetRun()
    setCoverage({current:0, needed: bridges[sel]?.quantity_required || 1})
    try {
      const r = await fetch(`${API}/negotiations/trigger`,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({bridge_index:sel, use_llm:false})}).then(r=>r.json())
      setNegId(r.neg_id); negRef.current = r.neg_id
      setCoverage(p=>({...p, needed:r.units_needed||1}))
      connect(r.neg_id)
    } catch { setStatus('idle'); alert('Backend not running — start the FastAPI server.') }
  }

  const confirmDonor = async(uid, ok)=>{
    const id = negRef.current
    if(!id || !uid) return
    setActed(a=>({...a, [uid]: ok?'confirmed':'declined'}))
    await fetch(`${API}/negotiations/${id}/${ok?'confirm':'decline'}`,{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({user_id:uid, confirmed:ok})}).catch(()=>{})
  }

  const injectDecline = async()=>{
    if(!negId) return
    const cms = messages.filter(m=>m.action==='REQUEST_HUMAN_CONFIRM')
    const last = cms[cms.length-1]
    const uid = last?.from?.split(':')[1]
    if(uid) await fetch(`${API}/negotiations/${negId}/decline`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:uid, confirmed:false})})
  }

  const steps = ['BROADCAST','RESPONSES','RESOLUTION','CONFIRM']
  return (
    <div className="console-layout">
      <div className="console-feed">
        <div className="console-header">
          <h2>Negotiation Floor</h2>
          {status==='running' && <span className="chip chip-red live-pill"><span className="dot"/>LIVE</span>}
          {status==='done' && <span className={`chip ${coverage.current>=coverage.needed?'chip-green':coverage.current>0?'chip-amber':'chip-red'}`}>
            {coverage.current>=coverage.needed?'COVERED':coverage.current>0?'PARTIAL':'FAILED'}</span>}
          {negId && <span className="msg-ts" style={{marginLeft:'auto'}}>{negId}</span>}
        </div>

        <div className="console-messages">
          {messages.length===0 && (
            <div className="floor-hero">
              <div className="hero-badge">{mode==='request'?'🚑 New blood request':'🩸 Negotiation Floor'}</div>
              <h1 className="hero-title">{mode==='request'?'What does the patient need?':'Give every donor a bot of their own.'}</h1>
              <p className="hero-sub">{mode==='request'
                ? 'We find every compatible donor within 10 km of the hospital and their Proxy agents respond live. Emergency alerts every donor at once — SMS now, a phone call if ignored.'
                : "Trigger a transfusion need and watch each donor's Proxy negotiate and protect its human — live."}</p>

              {mode==='request' ? (
                <div className="hero-bridge" style={{textAlign:'left'}}>
                  <div className="intake-grid">
                    <label>Blood group needed
                      <select className="input" value={intake.blood_group} onChange={e=>setIntake({...intake,blood_group:e.target.value})}>
                        {['O+','O-','A+','A-','B+','B-','AB+','AB-'].map(g=><option key={g}>{g}</option>)}</select></label>
                    <label>Hospital
                      <select className="input" value={intake.hospital_id} onChange={e=>setIntake({...intake,hospital_id:e.target.value})}>
                        {hospitals.map(h=><option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
                    <label>Units needed
                      <input className="input" type="number" min="1" value={intake.units} onChange={e=>setIntake({...intake,units:Number(e.target.value)})}/></label>
                    <label>Needed in (days)
                      <input className="input" type="number" min="0" value={intake.days} onChange={e=>setIntake({...intake,days:Number(e.target.value)})}/></label>
                    <label>Time
                      <input className="input" value={intake.time} onChange={e=>setIntake({...intake,time:e.target.value})}/></label>
                    <label>Search radius (km)
                      <input className="input" type="number" min="1" value={intake.radius_km} onChange={e=>setIntake({...intake,radius_km:Number(e.target.value)})}/></label>
                  </div>
                  <div className="pref-row" style={{borderBottom:'none',padding:'4px 0'}}>
                    <div><div style={{fontWeight:700,fontSize:13}}>🚨 Emergency</div>
                      <div style={{fontSize:11,color:'var(--text-muted)'}}>Alert ALL matched donors at once</div></div>
                    <div className={`toggle ${intake.emergency?'on':''}`} onClick={()=>setIntake({...intake,emergency:!intake.emergency})}/>
                  </div>
                  <button className="btn btn-primary hero-cta" onClick={submitIntake} disabled={status==='running'}>🚑 Find donors within {intake.radius_km} km</button>
                </div>
              ) : (bridges[sel] && (
                <div className="hero-bridge">
                  <div className="hb-row">
                    <span className={`chip chip-${bridges[sel].health_label==='green'?'green':bridges[sel].health_label==='amber'?'amber':'red'}`}>{bridges[sel].health_label?.toUpperCase()}</span>
                    <span className="hb-blood">{bridges[sel].bridge_blood_group}</span>
                    <span className="hb-meta">{bridges[sel].quantity_required} unit(s) · {bridges[sel].days_to_next_transfusion}d away · {bridges[sel].roster_size} donors</span>
                  </div>
                  <button className="btn btn-primary hero-cta" onClick={trigger} disabled={status==='running'}>⚡ Trigger this negotiation</button>
                </div>
              ))}

              <div className="hero-steps">
                {(mode==='request'
                  ? [['1','Match','Donors within 10 km, compatible'],['2','Alert','SMS → call if ignored'],['3','Confirm','Details appear on the right →']]
                  : [['1','Trigger','Proxies fan out & negotiate'],['2','Confirm','Tap ✓ under a booking here'],['3','Covered','Coverage gauge fills']]
                ).map(([n,t,d])=>(
                  <div className="hero-step" key={n}><span className="hs-num">{n}</span><div><div className="hs-t">{t}</div><div className="hs-d">{d}</div></div></div>
                ))}
              </div>
              <div style={{marginTop:6}}>
                <button className="nav-tab" onClick={()=>setMode(mode==='request'?'bridge':'request')} style={{fontSize:12}}>
                  {mode==='request'?'↔ Switch to bridge-trigger mode':'↔ Switch to patient-request mode'}
                </button>
              </div>
            </div>
          )}
          {messages.map(m=>(
            <div className="msg-row" key={m._id}>
              <div className={`msg-avatar ${avatarType(m.from_agent||m.from)}`}>{avatarLabel(m.from_agent||m.from)}</div>
              <div className="msg-body">
                <div className="msg-header">
                  <span className="msg-from">{(m.from_agent||m.from||'?').slice(0,30)}</span>
                  {m.action && <span className={`chip ${ACTION_CHIP[m.action]||'chip-gray'}`}>{m.action}</span>}
                  {m.round!==undefined && <span className="round-badge">R{m.round}</span>}
                  <span className="msg-ts">{timeStr(m.ts)}</span>
                </div>
                <div className={`msg-bubble ${['ESCALATE','WITHDRAW'].includes(m.action)?'system':''}`}>
                  {m.say || m.action}
                  {(()=>{ const meta = parseMeta(m.meta)
                    if(!meta || (m.from_agent||m.from||'').startsWith('proxy')===false) return null
                    const chips=[]
                    if(meta.distance_km!=null) chips.push(['📍', `${Math.round(meta.distance_km)} km`])
                    chips.push(meta.eligible?['✅','eligible']:['⏳',`${meta.days_to_eligible}d to eligible`])
                    if(meta.fatigue!=null) chips.push([meta.fatigue>0.7?'🛡️':'😌', `fatigue ${Math.round(meta.fatigue*100)}%`])
                    if(meta.propensity!=null) chips.push(['⭐', `prop ${meta.propensity}`])
                    return <div className="reason-line">
                      {chips.map((c,i)=><span className="reason-chip" key={i}>{c[0]} {c[1]}</span>)}
                      {meta.reason && <span className="reason-why">“{meta.reason}”</span>}
                    </div>
                  })()}
                  {m.params && typeof m.params==='object' && Object.keys(m.params).length>0 &&
                    <div className="params">{Object.entries(m.params).map(([k,v])=>`${k}: ${v}`).join('  ·  ')}</div>}
                  {m.action==='REQUEST_HUMAN_CONFIRM' && (()=>{
                    const uid=(m.from_agent||m.from)?.split(':')[1]
                    const state=acted[uid]
                    if(state) return <div style={{marginTop:8}}><span className={`chip ${state==='confirmed'?'chip-green':'chip-red'}`}>{state==='confirmed'?'✓ confirmed':'✗ declined'}</span></div>
                    return <div className="confirm-inline">
                      <button className="wa-btn confirm" onClick={()=>confirmDonor(uid,true)}>✓ Confirm</button>
                      <button className="wa-btn decline" onClick={()=>confirmDonor(uid,false)}>✗ Decline</button>
                    </div>
                  })()}
                </div>
              </div>
            </div>
          ))}
          {typing && <div className="msg-row"><div className="msg-avatar proxy">P</div>
            <div className="msg-body"><div className="msg-bubble"><div className="typing-dots"><span/><span/><span/></div></div></div></div>}
          <div ref={end}/>
        </div>

        <div className="console-controls">
          {mode==='request' ? (
            <>
              <button className="btn btn-primary" onClick={()=>{setMessages([]); setStatus('idle')}} disabled={status==='running'}>🚑 New patient request</button>
              {reqInfo && <span className="hb-meta" style={{flex:1}}>{reqInfo.emergency?'🚨 ':''}{reqInfo.blood_group} · {reqInfo.hospital} · contacting {reqInfo.contacting} of {reqInfo.matched} matched</span>}
            </>
          ) : (
            <>
              <select className="bridge-select" value={sel} onChange={e=>setSel(Number(e.target.value))}>
                {bridges.map((b,i)=>(
                  <option key={b.bridge_id} value={i}>
                    [{b.health_label?.toUpperCase()}] {b.bridge_blood_group} · {b.quantity_required}u · {b.days_to_next_transfusion}d · {b.roster_size} donors
                  </option>
                ))}
              </select>
              <button className="btn btn-primary" onClick={trigger} disabled={status==='running'}>⚡ Trigger Need</button>
            </>
          )}
          <button className="btn btn-danger" onClick={injectDecline} disabled={status!=='running'}>✗ Inject Decline</button>
        </div>
      </div>

      <div className="console-rail">
        {confirmedDonors.length>0 && (
          <div className="card" style={{borderColor:'rgba(16,185,129,0.4)'}}>
            <div className="card-title" style={{color:'var(--green-500)'}}>✅ Confirmed donors ({confirmedDonors.length})</div>
            <div style={{display:'flex',flexDirection:'column',gap:10}}>
              {confirmedDonors.map(d=>(
                <div key={d.user_id} className="donor-card">
                  <div className="donor-top">
                    <div className="donor-ava">{(d.name||'?').split(' ').map(x=>x[0]).join('').slice(0,2)}</div>
                    <div style={{flex:1}}>
                      <div className="donor-name">{d.name}</div>
                      <div className="donor-sub">{d.gender} · {d.age}y · DOB {d.dob}</div>
                    </div>
                    <span className="chip chip-red">{d.blood_group}{d.blood_group_simulated?'*':''}</span>
                  </div>
                  <div className="donor-rows">
                    <div><span>📍 Distance</span><b>{d.distance_km!=null?`${d.distance_km} km`:'—'}</b></div>
                    <div><span>🏙 City</span><b>{d.city||'—'}</b></div>
                    {d.phone && <div><span>📱 Phone</span><b>{d.phone}</b></div>}
                    {d.donations!=null && <div><span>🩸 Donations</span><b>{d.donations}</b></div>}
                  </div>
                </div>
              ))}
            </div>
            {confirmedDonors.some(d=>d.blood_group_simulated) && <div style={{fontSize:10,color:'var(--text-muted)',marginTop:8}}>* blood group simulated (not in dataset)</div>}
          </div>
        )}
        <div className="gauge-card">
          <div className="card-title">Coverage</div>
          <Gauge value={coverage.current} max={coverage.needed} label="units confirmed"/>
        </div>
        <div className="rail-grid">
          <div className="rail-stat"><div className="rail-label">Round</div><div className="rail-value">{round}<span style={{fontSize:14,color:'var(--text-muted)'}}>/3</span></div></div>
          <div className="rail-stat"><div className="rail-label">Messages</div><div className="rail-value">{messages.length}</div></div>
        </div>
        <div className="card">
          <div className="card-title">Protocol</div>
          <div className="protocol-steps">
            {steps.map((s,i)=>(
              <div key={s} className={`pstep ${round>i?'done':round===i?'active':''}`}>
                <span className="num">{round>i?'✓':i}</span>{s}
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-title">Network Impact</div>
          <div className="rail-grid">
            <div className="rail-stat"><div className="rail-label">Negotiations</div><div className="rail-value" style={{fontSize:18}}>{stats?.negotiations_run||0}</div></div>
            <div className="rail-stat"><div className="rail-label">Notifs sent</div><div className="rail-value" style={{fontSize:18,color:'var(--green-400)'}}>{stats?.human_notifications||0}</div></div>
          </div>
          <div className="rail-stat" style={{marginTop:10}}>
            <div className="rail-label">Calls saved vs legacy</div>
            <div className="rail-value" style={{fontSize:22,color:'var(--amber-400)'}}>{fmt(stats?.calls_saved)}</div>
            <div className="rail-sub">{fmt(stats?.legacy_calls_equivalent)} calls the old way</div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ══════════════════════════════════════════════════════
// BRIDGE BOARD
// ══════════════════════════════════════════════════════
function Board({ bridges, recovery, churn }) {
  const [sel, setSel] = useState(null)
  const g = bridges.filter(b=>b.health_label==='green').length
  const a = bridges.filter(b=>b.health_label==='amber').length
  const r = bridges.filter(b=>b.health_label==='red').length
  return (
    <div className="board">
      <div><div className="section-title">Bridge Coverage Board</div>
        <div className="section-sub">80 real Blood Bridges · health = eligible supply over the next 90 days vs forecast demand</div></div>
      <div className="board-stats">
        <div className="stat-card" style={{color:'var(--text-primary)'}}><div className="label">Bridges</div><div className="value">{bridges.length}</div><div className="sub">monitored</div></div>
        <div className="stat-card" style={{color:'var(--green-400)'}}><div className="label">Healthy</div><div className="value" style={{color:'var(--green-400)'}}>{g}</div><div className="sub">green supply</div></div>
        <div className="stat-card" style={{color:'var(--amber-400)'}}><div className="label">At risk</div><div className="value" style={{color:'var(--amber-400)'}}>{a}</div><div className="sub">amber</div></div>
        <div className="stat-card" style={{color:'var(--red-400)'}}><div className="label">Critical</div><div className="value" style={{color:'var(--red-400)'}}>{r}</div><div className="sub">red — act now</div></div>
        <div className="stat-card" style={{color:'var(--teal-400)'}}><div className="label">Recovery pipeline</div><div className="value" style={{color:'var(--teal-400)'}}>{recovery?.active||0}</div><div className="sub">deferred donors retained</div></div>
      </div>
      {churn?.auc_mean && <div className="banner info">🧠 Churn model live · <b>{churn.model}</b> · AUC <b>{churn.auc_mean}</b> on {fmt(churn.n_positives)} inactive / {fmt(churn.n_negatives)} active — flags at-risk roster for preemptive recruitment.</div>}

      <div className="bridge-grid">
        {bridges.map(b=>(
          <div key={b.bridge_id} className={`bridge-tile ${b.health_label}`}
            onClick={()=>setSel(sel?.bridge_id===b.bridge_id?null:b)}>
            <div className="tile-top">
              <div><div className="tile-blood">{b.bridge_blood_group}</div>
                <div className="tile-days-label">{b.roster_size} donors · {b.quantity_required}u needed</div></div>
              <div style={{textAlign:'right'}}><div className="tile-days">{b.days_to_next_transfusion}</div>
                <div className="tile-days-label">days out</div></div>
            </div>
            <div className="tile-meta">
              <span className={`chip chip-${b.health_label==='green'?'green':b.health_label==='amber'?'amber':'red'}`}>{Math.round(b.health_score*100)}% health</span>
              {b.negotiation_stats && Object.entries(b.negotiation_stats).map(([k,v])=>
                <span key={k} className="chip chip-gray">{k} {v}</span>)}
            </div>
          </div>
        ))}
      </div>

      {sel && (
        <div className="card drill">
          <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:14}}>
            <div className="section-title">{sel.bridge_blood_group} Bridge</div>
            <span className={`chip chip-${sel.health_label==='green'?'green':sel.health_label==='amber'?'amber':'red'}`}>{Math.round(sel.health_score*100)}% health</span>
          </div>
          <div style={{display:'grid',gridTemplateColumns:'repeat(3,1fr)',gap:16,marginBottom:16}}>
            <div className="rail-stat"><div className="rail-label">Next transfusion</div><div className="pref-value">{sel.next_transfusion_date?.slice(0,10)||'—'}</div></div>
            <div className="rail-stat"><div className="rail-label">Units required</div><div className="pref-value">{sel.quantity_required}</div></div>
            <div className="rail-stat"><div className="rail-label">Roster</div><div className="pref-value">{sel.roster_size} donors</div></div>
          </div>
          <div className="card-title">Roster · ranked by readiness</div>
          <div style={{display:'flex',flexDirection:'column',gap:7,maxHeight:260,overflowY:'auto'}}>
            {(sel.roster||[]).slice(0,12).map(d=>(
              <div key={d.user_id} className="roster-row">
                <span className={`chip ${d.eligible_now?'chip-green':'chip-red'}`}>{d.eligible_now?'eligible':`${d.days_to_eligible}d`}</span>
                <span style={{fontSize:12,fontFamily:'var(--font-mono)',color:'var(--text-secondary)'}}>{d.user_id.slice(0,14)}…</span>
                <span style={{marginLeft:'auto',fontSize:11,color:'var(--text-muted)'}}>prop {d.propensity}</span>
                <span className={`chip ${d.fatigue_score>0.7?'chip-red':d.fatigue_score>0.4?'chip-amber':'chip-green'}`}>fatigue {Math.round(d.fatigue_score*100)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ══════════════════════════════════════════════════════
// PHONE SIMULATOR  (pre-screen + confirm + screening opt-in)
// ══════════════════════════════════════════════════════
function Setup(){
  const [twi, setTwi] = useState({donor_phone:'', patient_phone:'', public_base:'', account_sid:'', auth_token:'', call_from:'', enabled:false})
  const [st, setSt] = useState(null)
  const [phones, setPhones] = useState([])
  const [newPhone, setNewPhone] = useState('')
  const [pol, setPol] = useState(null)
  const [aws, setAws] = useState(null)
  const load = ()=>{
    fetch(`${API}/twilio/config`).then(r=>r.json()).then(s=>{setSt(s); setTwi(t=>({...t, donor_phone:s.donor_phone||t.donor_phone, patient_phone:s.patient_phone||t.patient_phone, public_base:s.public_base||t.public_base, call_from:s.call_from||t.call_from, enabled:s.enabled}))}).catch(()=>{})
    fetch(`${API}/live-phones`).then(r=>r.json()).then(d=>setPhones(d.phones||[])).catch(()=>{})
    fetch(`${API}/policy`).then(r=>r.json()).then(setPol).catch(()=>{})
    fetch(`${API}/aws/status`).then(r=>r.json()).then(setAws).catch(()=>{})
  }
  useEffect(()=>{ load() },[])
  const savePol = async(p)=>{ setPol(p); await fetch(`${API}/policy`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}).catch(()=>{}) }
  const saveAws = async()=>{ await fetch(`${API}/aws/config`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bucket:aws?.bucket,region:aws?.region})}).catch(()=>{}); setTimeout(load,400) }
  const testAws = async()=>{ await fetch(`${API}/aws/test`,{method:'POST'}).catch(()=>{}); setTimeout(load,1000) }
  const save = async()=>{ const body={...twi}; if(!body.account_sid) delete body.account_sid; if(!body.auth_token) delete body.auth_token; await fetch(`${API}/twilio/config`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).catch(()=>{}); setTimeout(load,500) }
  const autodetect = async()=>{ const r=await fetch(`${API}/twilio/autodetect-ngrok`,{method:'POST'}).then(r=>r.json()).catch(()=>({})); if(r.public_base) setTwi(t=>({...t,public_base:r.public_base})); setTimeout(load,400) }
  const test = async()=>{ await fetch(`${API}/twilio/test`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({donor_phone:twi.donor_phone})}).catch(()=>{}); setTimeout(load,1500) }
  const addPhone = async()=>{ if(!newPhone) return; await fetch(`${API}/live-phones`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:newPhone})}).catch(()=>{}); setNewPhone(''); setTimeout(load,400) }

  return (
    <div className="prevention">
      <div><div className="section-title">Setup — real phone alerts</div>
        <div className="section-sub">Everything runs from the Floor. This is just the one-time wiring for real SMS + call escalation.</div></div>
      <div className="prev-hero">
        <div className="card">
          <div className="card-title">📞 Twilio (donor SMS → call escalation)</div>
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            <label className="fld">Public URL (ngrok)<div style={{display:'flex',gap:8}}>
              <input className="input" value={twi.public_base} onChange={e=>setTwi({...twi,public_base:e.target.value})} placeholder="https://xxxx.ngrok-free.app"/>
              <button className="wa-btn ghost" style={{flex:'0 0 auto',minWidth:110}} onClick={autodetect}>Auto-detect</button></div></label>
            <label className="fld">Twilio number (SMS + calls)<input className="input" value={twi.call_from} onChange={e=>setTwi({...twi,call_from:e.target.value})} placeholder="+1..."/></label>
            <label className="fld">Account SID<input className="input" value={twi.account_sid} onChange={e=>setTwi({...twi,account_sid:e.target.value})} placeholder={st?.configured?'saved (leave blank to keep)':'AC...'}/></label>
            <label className="fld">Auth Token<input className="input" type="password" value={twi.auth_token} onChange={e=>setTwi({...twi,auth_token:e.target.value})} placeholder={st?.configured?'saved (leave blank to keep)':'token'}/></label>
            <label className="fld">Patient phone (gets confirmation SMS)<input className="input" value={twi.patient_phone} onChange={e=>setTwi({...twi,patient_phone:e.target.value})}/></label>
          </div>
          <div className="pref-row" style={{marginTop:8}}>
            <div><div className="pref-label">Enable real alerts</div><div style={{fontSize:11,color:'var(--text-muted)'}}>Off = simulate in-app on the Floor</div></div>
            <div className={`toggle ${twi.enabled?'on':''}`} onClick={()=>setTwi({...twi,enabled:!twi.enabled})}/>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button className="wa-btn confirm" onClick={save}>Save</button>
            <button className="wa-btn ghost" onClick={test}>Send test SMS</button>
          </div>
          {st && <div style={{marginTop:10,fontSize:11,color:'var(--text-muted)',lineHeight:1.8}}>
            <div>Twilio: <b style={{color:st.configured?'var(--green-500)':'var(--amber-400)'}}>{st.configured?'configured':'creds not set'}</b> · alerts <b style={{color:st.enabled?'var(--green-500)':'var(--amber-400)'}}>{st.enabled?'ON':'OFF'}</b></div>
            <div>Escalates to a call after {st.escalate_after}s of no reply.</div>
            {st.last_event?.status && st.last_event.status!=='none' && <div>Last: <span className={`chip ${(st.last_event.status||'').includes('error')?'chip-red':(st.last_event.status||'').includes('mock')?'chip-amber':'chip-green'}`}>{st.last_event.status}</span></div>}
          </div>}
        </div>
        <div className="card">
          <div className="card-title">📇 Donor numbers (live_phones.json)</div>
          <div style={{fontSize:12,color:'var(--text-secondary)',marginBottom:10}}>These real numbers receive the SMS + escalation call. The first matched donors are assigned these.</div>
          <div style={{display:'flex',flexDirection:'column',gap:7,marginBottom:10}}>
            {phones.map((p,i)=><div key={i} className="roster-row"><span className="camp-pin" style={{background:'linear-gradient(135deg,var(--red-400),var(--red-600))'}}>{i+1}</span><b style={{fontFamily:'var(--font-mono)'}}>{p}</b></div>)}
            {phones.length===0 && <div style={{color:'var(--text-muted)',fontSize:12}}>No numbers yet.</div>}
          </div>
          <div style={{display:'flex',gap:8}}>
            <input className="input" value={newPhone} onChange={e=>setNewPhone(e.target.value)} placeholder="+9198XXXXXXXX"/>
            <button className="wa-btn confirm" style={{flex:'0 0 auto',minWidth:90}} onClick={addPhone}>Add</button>
          </div>
        </div>
      </div>

      <div className="prev-hero">
        {/* Donor-contact conditions = the agent rules */}
        <div className="card">
          <div className="card-title">🧠 Donor conditions (agent rules)</div>
          <div style={{fontSize:12,color:'var(--text-secondary)',marginBottom:12}}>The Proxy agents enforce these — they decide whether to disturb a donor at all. Great to show judges.</div>
          {pol ? (
            <div style={{display:'flex',flexDirection:'column',gap:12}}>
              <div>
                <div className="pref-row" style={{borderBottom:'none',padding:0}}><span className="pref-label">Max distance</span><b className="pref-value">{pol.max_distance_km} km</b></div>
                <input type="range" min="1" max="50" value={pol.max_distance_km} style={{width:'100%'}} onChange={e=>savePol({...pol,max_distance_km:Number(e.target.value)})}/>
              </div>
              <div>
                <div className="pref-row" style={{borderBottom:'none',padding:0}}><span className="pref-label">Skip if fatigue above</span><b className="pref-value">{Math.round(pol.max_fatigue*100)}%</b></div>
                <input type="range" min="0" max="100" value={Math.round(pol.max_fatigue*100)} style={{width:'100%'}} onChange={e=>savePol({...pol,max_fatigue:Number(e.target.value)/100})}/>
              </div>
              <div>
                <div className="pref-row" style={{borderBottom:'none',padding:0}}><span className="pref-label">Min willingness (propensity)</span><b className="pref-value">{Math.round(pol.min_propensity*100)}%</b></div>
                <input type="range" min="0" max="100" value={Math.round(pol.min_propensity*100)} style={{width:'100%'}} onChange={e=>savePol({...pol,min_propensity:Number(e.target.value)/100})}/>
              </div>
              <div className="pref-row"><div><div className="pref-label">Only eligible donors</div><div style={{fontSize:11,color:'var(--text-muted)'}}>Emergency overrides this</div></div>
                <div className={`toggle ${pol.only_eligible?'on':''}`} onClick={()=>savePol({...pol,only_eligible:!pol.only_eligible})}/></div>
              <div className="pref-row"><div><div className="pref-label">Escalate to a call</div><div style={{fontSize:11,color:'var(--text-muted)'}}>If donor ignores the SMS</div></div>
                <div className={`toggle ${pol.enable_call?'on':''}`} onClick={()=>savePol({...pol,enable_call:!pol.enable_call})}/></div>
              <div>
                <div className="pref-row" style={{borderBottom:'none',padding:0}}><span className="pref-label">Call after</span><b className="pref-value">{pol.escalate_after}s</b></div>
                <input type="range" min="5" max="120" value={pol.escalate_after} style={{width:'100%'}} onChange={e=>savePol({...pol,escalate_after:Number(e.target.value)})}/>
              </div>
            </div>
          ) : <div style={{color:'var(--text-muted)',fontSize:12}}>Loading…</div>}
        </div>

        {/* Amazon S3 audit */}
        <div className="card">
          <div className="card-title">☁️ Amazon S3 audit trail</div>
          <div style={{fontSize:12,color:'var(--text-secondary)',marginBottom:10}}>Every negotiation's transcript is written to S3 — your AWS proof + responsible-data audit.</div>
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            <input className="input" value={aws?.bucket||''} onChange={e=>setAws({...aws,bucket:e.target.value})} placeholder="S3 bucket name"/>
            <input className="input" value={aws?.region||''} onChange={e=>setAws({...aws,region:e.target.value})} placeholder="region (e.g. ap-south-1)"/>
          </div>
          <div style={{display:'flex',gap:8,marginTop:10}}>
            <button className="wa-btn confirm" onClick={saveAws}>Save</button>
            <button className="wa-btn ghost" onClick={testAws}>Test upload</button>
          </div>
          {aws && <div style={{marginTop:10,fontSize:11,color:'var(--text-muted)',lineHeight:1.8}}>
            <div>AWS creds: <b style={{color:aws.aws_creds?'var(--green-500)':'var(--amber-400)'}}>{aws.aws_creds?'detected':'not set → mock'}</b> · bucket <b>{aws.bucket||'—'}</b></div>
            <div>Objects written: <b>{aws.last?.count||0}</b>{aws.last?.key?` · last: ${aws.last.key}`:''}</div>
            {aws.last?.status && aws.last.status!=='none' && <div>Last: <span className={`chip ${aws.last.status==='uploaded'?'chip-green':aws.last.status==='error'?'chip-red':'chip-amber'}`}>{aws.last.status}</span></div>}
          </div>}
        </div>
      </div>
    </div>
  )
}

// ══════════════════════════════════════════════════════
// PREVENTION TAB  (Module B flywheel)
// ══════════════════════════════════════════════════════
function Prevention({ recovery }) {
  const [proj, setProj] = useState(null)
  const [camps, setCamps] = useState(null)
  const [screen, setScreen] = useState(null)
  const [bridges, setBridges] = useState([])

  useEffect(()=>{
    fetch(`${API}/prevention/projection`).then(r=>r.json()).then(setProj).catch(()=>{})
    fetch(`${API}/prevention/camps`).then(r=>r.json()).then(setCamps).catch(()=>{})
    fetch(`${API}/screening/stats`).then(r=>r.json()).then(setScreen).catch(()=>{})
    fetch(`${API}/bridges`).then(r=>r.json()).then(d=>setBridges(d.bridges||[])).catch(()=>{})
    const t = setInterval(()=>fetch(`${API}/screening/stats`).then(r=>r.json()).then(setScreen).catch(()=>{}), 4000)
    return ()=>clearInterval(t)
  },[])

  // normalize lat/lon to minimap box
  const pts = bridges.filter(b=>b.centroid_lat&&b.centroid_lon).map(b=>({lat:b.centroid_lat,lon:b.centroid_lon}))
  const all = [...pts, ...((camps?.camps)||[]).map(c=>({lat:c.lat,lon:c.lon}))]
  const bounds = all.length ? {
    minLat:Math.min(...all.map(p=>p.lat)), maxLat:Math.max(...all.map(p=>p.lat)),
    minLon:Math.min(...all.map(p=>p.lon)), maxLon:Math.max(...all.map(p=>p.lon)),
  } : null
  const xy = (lat,lon)=> !bounds ? {x:'50%',y:'50%'} : {
    x: `${6 + 88*(lon-bounds.minLon)/((bounds.maxLon-bounds.minLon)||1)}%`,
    y: `${94 - 88*(lat-bounds.minLat)/((bounds.maxLat-bounds.minLat)||1)}%`,
  }

  return (
    <div className="prevention">
      <div><div className="section-title">Prevention Flywheel</div>
        <div className="section-sub">Every donor is already at a blood draw — the largest voluntary screening funnel in India, hiding in plain sight. Demand elimination, not just supply.</div></div>

      <div className="prev-hero">
        <div className="card">
          <div className="card-title">Projected annual payoff · from the real bridge dataset</div>
          <div className="flow-arrow" style={{marginBottom:16}}>
            <div className="flow-node"><div className="n">{fmt(proj?.screened_per_year)}</div><div className="t">screened/yr</div></div>
            <span className="flow-sep">→</span>
            <div className="flow-node"><div className="n">{fmt(proj?.carriers_found)}</div><div className="t">carriers found</div></div>
            <span className="flow-sep">→</span>
            <div className="flow-node"><div className="n">{fmt(proj?.at_risk_couples)}</div><div className="t">at-risk couples</div></div>
            <span className="flow-sep">→</span>
            <div className="flow-node"><div className="n">{fmt(proj?.prevented_births_per_yr)}</div><div className="t">births prevented</div></div>
          </div>
          <div style={{height:200}}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={proj?.cumulative_10yr||[]} margin={{top:6,right:8,left:-12,bottom:0}}>
                <defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#0d9488" stopOpacity={0.35}/><stop offset="100%" stopColor="#0d9488" stopOpacity={0}/>
                </linearGradient></defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(80,60,40,0.1)"/>
                <XAxis dataKey="year" tick={{fill:'#6f6557',fontSize:11}} tickFormatter={y=>`Y${y}`}/>
                <YAxis tick={{fill:'#6f6557',fontSize:11}} tickFormatter={v=>v>=1000?`${(v/1000).toFixed(0)}k`:v}/>
                <Tooltip contentStyle={{background:'#fff',border:'1px solid rgba(80,60,40,0.15)',borderRadius:10,fontSize:12,color:'#2b2520'}}
                  formatter={v=>[fmt(v),'transfusions averted']} labelFormatter={y=>`Year ${y}`}/>
                <Area type="monotone" dataKey="transfusions_averted" stroke="#0d9488" strokeWidth={2.5} fill="url(#g1)"/>
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="banner success" style={{marginTop:12}}>💡 Each prevented birth ≈ {proj?.transfusions_per_life||600} transfusions that never need coordinating. <b>{fmt(proj?.transfusions_averted_per_yr)}</b> averted in year one alone.</div>
        </div>

        <div className="flywheel-stats">
          <div className="fly-stat"><div className="v">{fmt(screen?.screenings_scheduled||0)}</div><div className="l">Screenings scheduled (live, from the phone opt-in)</div></div>
          <div className="fly-stat"><div className="v">{proj?.carrier_rate?`${(proj.carrier_rate*100).toFixed(1)}%`:'3.5%'}</div><div className="l">National carrier rate (simulated, labeled)</div></div>
          <div className="fly-stat"><div className="v">{fmt(recovery?.active||0)}</div><div className="l">Donors in recovery pipeline (Module A)</div></div>
          <div className="fly-stat"><div className="v">{fmt(proj?.donor_pool)}</div><div className="l">Active donor pool = the screening funnel</div></div>
        </div>
      </div>

      <div className="prev-hero">
        <div className="card">
          <div className="card-title">Camp placement · k-means on 7,033 donor locations</div>
          <div className="minimap">
            {pts.map((p,i)=>{const {x,y}=xy(p.lat,p.lon);return <div key={i} className="dot" style={{left:x,top:y}}/>})}
            {(camps?.camps||[]).map(c=>{const {x,y}=xy(c.lat,c.lon);return <div key={c.camp} className="camp" style={{left:x,top:y}} title={`Camp ${c.camp}`}/>})}
          </div>
          {camps && <div className="banner info" style={{marginTop:12}}>📍 Place <b>{camps.k}</b> camps → reach <b>{camps.coverage_pct}%</b> of the active pool within {camps.radius_km} km.</div>}
        </div>
        <div className="card">
          <div className="card-title">Recommended camps</div>
          <div style={{display:'flex',flexDirection:'column',gap:8}}>
            {(camps?.camps||[]).map(c=>(
              <div className="camp-row" key={c.camp}>
                <div className="camp-pin">{c.camp}</div>
                <div style={{flex:1}}><div style={{fontSize:13,fontWeight:600}}>Camp {c.camp}</div>
                  <div style={{fontSize:11,color:'var(--text-muted)',fontFamily:'var(--font-mono)'}}>{c.lat.toFixed(3)}, {c.lon.toFixed(3)}</div></div>
                <span className="chip chip-teal">{fmt(c.donors_in_cluster)} donors</span>
              </div>
            ))}
            {!camps && <div style={{color:'var(--text-muted)',fontSize:12}}>Loading camp model…</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

// ══════════════════════════════════════════════════════
// ROOT
// ══════════════════════════════════════════════════════
export default function App(){
  const [tab, setTab] = useState('console')
  const [bridges, setBridges] = useState([])
  const [stats, setStats] = useState(null)
  const [recovery, setRecovery] = useState(null)
  const [churn, setChurn] = useState(null)
  const [questions, setQuestions] = useState([])
  const [simToday, setSimToday] = useState('')

  const refresh = useCallback(()=>{
    fetch(`${API}/stats`).then(r=>r.json()).then(s=>{setStats(s); if(s.sim_today) setSimToday(s.sim_today.slice(0,10))}).catch(()=>{})
    fetch(`${API}/recovery`).then(r=>r.json()).then(setRecovery).catch(()=>{})
    fetch(`${API}/bridges`).then(r=>r.json()).then(d=>setBridges(d.bridges||[])).catch(()=>{})
  },[])

  useEffect(()=>{
    refresh()
    fetch(`${API}/churn/meta`).then(r=>r.json()).then(setChurn).catch(()=>{})
    fetch(`${API}/prescreen/questions`).then(r=>r.json()).then(d=>setQuestions(d.questions||[])).catch(()=>{})
    const t = setInterval(refresh, 8000)
    return ()=>clearInterval(t)
  },[refresh])

  const tabs = [['console','⚡ Floor'],['board','🩸 Bridge Board'],['prevention','🧬 Prevention'],['setup','⚙️ Setup']]
  return (
    <div className="app">
      <nav className="top-nav">
        <div className="logo"><div className="logo-dot"/><span className="brand-grad">RAKTA-SETU</span></div>
        <div className="nav-tabs">
          {tabs.map(([id,l])=><button key={id} className={`nav-tab ${tab===id?'active':''}`} onClick={()=>setTab(id)}>{l}</button>)}
        </div>
        <div className="nav-right">
          {simToday && <span className="sim-badge">SIM CLOCK · {simToday}</span>}
          <span className="chip chip-gray">{bridges.length} bridges</span>
        </div>
      </nav>
      <div className="main">
        {tab==='console'    && <Console bridges={bridges} stats={stats} refresh={refresh}/>}
        {tab==='board'      && <Board bridges={bridges} recovery={recovery} churn={churn}/>}
        {tab==='prevention' && <Prevention recovery={recovery}/>}
        {tab==='setup'      && <Setup/>}
      </div>
    </div>
  )
}
