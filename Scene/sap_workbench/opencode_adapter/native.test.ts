import {expect, test} from "bun:test"
import {mkdtemp, mkdir, writeFile, cp, rm, access} from "node:fs/promises"
import {tmpdir} from "node:os"
import {join, dirname} from "node:path"
import {Database} from "bun:sqlite"

const root = process.env.SAP_OPENCODE_ROOT
const skillNames = ["openspec-explore", "openspec-propose", "openspec-apply-change", "openspec-archive-change"]

test.skipIf(!root)("native SAP host discovers global/project configuration and executes native OpenSpec tools", async () => {
  const dir = await mkdtemp(join(tmpdir(), "sap-native-config-"))
  const config = join(dir, "config/opencode"), project = join(dir, "project"), runtime = join(dir, "private")
  const sourceSkills = join(dirname(dirname(root!)), "sapwork/.opencode/skills")
  const token = crypto.randomUUID()
  const seen: string[] = []
  let turn = 0, child: ReturnType<typeof Bun.spawn> | undefined
  const provider = Bun.serve({hostname:"127.0.0.1",port:0,async fetch(request) {
    const body = await request.json()
    seen.push(...(body.tools ?? []).map((tool: {function:{name:string}}) => tool.function.name))
    turn++
    const call = turn === 1 ? {name:"skill",arguments:JSON.stringify({name:"openspec-explore"})}
      : turn === 2 ? {name:"bash",arguments:JSON.stringify({command:"openspec --version"})} : undefined
    const delta = call ? {role:"assistant",tool_calls:[{index:0,id:`call_native_${turn}`,type:"function",function:call}]}
      : {role:"assistant",content:"Native OpenSpec ready."}
    return new Response([
      {id:"native",object:"chat.completion.chunk",choices:[{index:0,delta,finish_reason:null}]},
      {id:"native",object:"chat.completion.chunk",choices:[{index:0,delta:{},finish_reason:call?"tool_calls":"stop"}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}},
    ].map(value=>`data: ${JSON.stringify(value)}\n\n`).join("")+"data: [DONE]\n\n",{headers:{"Content-Type":"text/event-stream"}})
  }})
  try {
    await mkdir(config,{recursive:true}); await mkdir(join(project,".opencode/skills"),{recursive:true})
    for(const name of skillNames) await cp(join(sourceSkills,name),join(project,".opencode/skills",name),{recursive:true})
    await mkdir(join(project,".opencode/commands"))
    await writeFile(join(project,".opencode/commands/opsx-explore.md"),"---\ndescription: Explore with OpenSpec\n---\nUse openspec-explore.\n")
    await mkdir(join(dir,"data/opencode"),{recursive:true})
    const connections = new Database(join(dir,"data/opencode/opencode-local.db"))
    connections.run("CREATE TABLE credential (id TEXT,integration_id TEXT,label TEXT,value TEXT,time_created INTEGER,time_updated INTEGER)")
    connections.query("INSERT INTO credential VALUES (?,?,?,?,?,?)").run("cred_fixture","deepseek","fixture",JSON.stringify({type:"key",key:"test-only"}),1,1)
    connections.run("CREATE TABLE private_fixture_history (text TEXT)")
    connections.run("INSERT INTO private_fixture_history VALUES ('must not inherit history')")
    connections.close()
    await writeFile(join(config,"opencode.json"),JSON.stringify({model:"probe/first",permission:{"*":"allow"},
      provider:{deepseek:{models:{"configured-alias":{id:"deepseek-chat",name:"Configured native alias",tool_call:true,limit:{context:64000,output:4096}}}},
      probe:{npm:"@ai-sdk/openai-compatible",options:{baseURL:`${provider.url}v1`,apiKey:"test-only"},
        models:{first:{name:"First",tool_call:true,limit:{context:64000,output:4096}},second:{name:"Second",tool_call:true,limit:{context:64000,output:4096}}}}}}))
    await writeFile(join(project,"opencode.json"),JSON.stringify({default_agent:"build",agent:{reviewer:{mode:"primary",prompt:"Review project files."}}}))
    child = Bun.spawn([Bun.which("bun")!,join(import.meta.dir,"server.ts")],{cwd:root!,stdin:"pipe",stdout:"pipe",stderr:"pipe",
      env:{PATH:process.env.PATH!,HOME:dir,TMPDIR:tmpdir(),XDG_CONFIG_HOME:join(dir,"config"),XDG_DATA_HOME:join(dir,"data"),
        XDG_CACHE_HOME:join(dir,"cache"),XDG_STATE_HOME:join(dir,"state"),OPENCODE_DISABLE_MODELS_FETCH:"1",OPENCODE_DISABLE_AUTOUPDATE:"1"}})
    let stderr = ""
    const errors = (async()=>{for await(const chunk of child!.stderr) stderr += new TextDecoder().decode(chunk)})()
    child.stdin.write(JSON.stringify({directory:runtime,root,token,displayMode:"iframe"})); child.stdin.end()
    let pending="", port=0
    for await(const chunk of child.stdout) {
      pending += new TextDecoder().decode(chunk)
      const lines=pending.split("\n");pending=lines.pop()!
      for(const line of lines) {try {const result=JSON.parse(line);if(Number.isInteger(result.port))port=result.port} catch {}}
      if(port)break
    }
    expect(port,stderr).toBeGreaterThan(0)
    const base=`http://127.0.0.1:${port}`,headers={Authorization:"Basic "+btoa("opencode:"+token)}
    const api = async(path:string,body?:unknown) => {
      const response=await fetch(base+path,{headers:{...headers,"Content-Type":"application/json"},method:body===undefined?"GET":"POST",body:body===undefined?undefined:JSON.stringify(body)})
      const text=await response.text();expect(response.status,text).toBeLessThan(300)
      return text?JSON.parse(text):undefined
    }
    expect((await fetch(base+"/api/health")).status).toBe(401)
    expect((await fetch(base+"/api/health",{headers:{Authorization:"Basic "+btoa("opencode:wrong")}})).status).toBe(401)
    const query="?location[directory]="+encodeURIComponent(project)
    let agents
    const ready=Date.now()+15000
    do {agents=await api("/api/agent"+query);if(agents.data.some((a:{id:string})=>a.id==="reviewer"))break;await Bun.sleep(50)}while(Date.now()<ready)
    expect(agents.data.map((a:{id:string})=>a.id)).toEqual(expect.arrayContaining(["build","plan","reviewer"]))
    const models=(await api("/api/model"+query)).data
    expect(models.filter((m:{providerID:string})=>m.providerID==="probe").map((m:{id:string})=>m.id)).toEqual(expect.arrayContaining(["first","second"]))
    expect(models.some((m:{providerID:string,id:string})=>m.providerID==="deepseek"&&m.id==="configured-alias")).toBe(true)
    expect((await api("/config?directory="+encodeURIComponent(project))).model).toBe("probe/first")
    expect((await api("/api/skill"+query)).data.map((s:{name:string})=>s.name)).toEqual(expect.arrayContaining(skillNames))
    expect((await api("/api/command"+query)).data.map((c:{name:string})=>c.name)).toContain("opsx-explore")
    const id="ses_native_"+crypto.randomUUID()
    await api("/api/session",{id,location:{directory:project},agent:"build",model:{providerID:"probe",id:"first"}})
    await api(`/api/session/${id}/prompt`,{prompt:{text:"Load openspec-explore then run openspec --version."}})
    const deadline=Date.now()+20000
    let context
    do {context=await api(`/api/session/${id}/context`);if(turn>=3&&!Object.keys((await api("/api/session/active")).data).length)break;await Bun.sleep(75)}while(Date.now()<deadline)
    const serialized=JSON.stringify(context)
    expect(turn,serialized).toBe(3)
    expect(seen).toContain("skill");expect(seen).toContain("bash");expect(seen).toContain("read")
    expect(serialized).toContain("skill_content");expect(serialized).toContain("Native OpenSpec ready.")
    expect(serialized).not.toContain("Unable to execute command")
    await access(join(runtime,"opencode.db"))
    const privateDatabase = new Database(join(runtime,"opencode.db"),{readonly:true})
    expect(privateDatabase.query("SELECT name FROM sqlite_master WHERE name='private_fixture_history'").get()).toBeNull()
    privateDatabase.close()
    expect(await Bun.file(join(runtime,"config/opencode.json")).exists()).toBe(false)
    child.kill();await child.exited;await errors
  } finally {
    if(child){child.kill();await child.exited}
    provider.stop(true);await rm(dir,{recursive:true,force:true})
  }
},60000)
