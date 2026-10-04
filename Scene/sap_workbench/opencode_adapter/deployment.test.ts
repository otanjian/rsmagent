import {expect, test} from "bun:test"
import {mkdtemp, mkdir, rm, readFile} from "node:fs/promises"
import {tmpdir} from "node:os"
import {join} from "node:path"

const root = process.env.SAP_OPENCODE_ROOT

test.skipIf(!root)("two deployed canonical hosts reject direct and cross-host credentials", async () => {
  const directory = await mkdtemp(join(tmpdir(), "sap-host-boundary-"))
  const processes: ReturnType<typeof Bun.spawn>[] = []
  const ports: number[] = []
  const tokens = [crypto.randomUUID(), crypto.randomUUID()]
  const project = join(directory,"shared-project")
  await mkdir(project)
  const deadEnd = Bun.serve({hostname:"127.0.0.1",port:0,fetch:()=>new Response("No model or bridge calls permitted",{status:503})})
  try {
    for (let index=0;index<2;index++) {
      const process = Bun.spawn([Bun.which("bun")!, join(import.meta.dir,"server.ts")], {
        cwd:root!, stdin:"pipe", stdout:"pipe", stderr:"ignore",
        env: Object.fromEntries(["PATH","HOME","TMPDIR","LANG"].flatMap(key=>globalThis.process.env[key] ? [[key,globalThis.process.env[key]!]]:[])),
      })
      processes.push(process)
      process.stdin.write(JSON.stringify({directory:join(directory,String(index)),root,token:tokens[index],
        modelURL:`${deadEnd.url}model`,model:"test-only",bridgeURL:`${deadEnd.url}bridge/`,service:`test-${index}`,
        displayMode:"screen"}))
      process.stdin.end()
      let pending=""
      for await (const chunk of process.stdout) {
        pending += new TextDecoder().decode(chunk)
        const lines = pending.split("\n")
        pending = lines.pop()!
        for (const line of lines) {
          try { const result=JSON.parse(line); if(Number.isInteger(result.port)) ports[index]=result.port } catch {}
        }
        if(ports[index]) break
      }
      expect(ports[index]).toBeGreaterThan(0)
      const saved=JSON.parse(await readFile(join(directory,String(index),"config/opencode.json"),"utf8"))
      expect(saved.permission.sap_mcp_read).toBe("allow")
      expect(saved.permission.sap_purchase_order_read).toBe("allow")
      expect(saved.permission["*"]).toBe("deny")
      expect(saved.permission.sap_page_read).toBe("allow")
    }
    for (let index=0;index<2;index++) {
      const url=`http://127.0.0.1:${ports[index]}/api/health`
      expect((await fetch(url)).status).toBe(401)
      expect((await fetch(url,{headers:{Authorization:`Bearer ${tokens[1-index]}`}})).status).toBe(401)
      expect((await fetch(url,{headers:{Authorization:`Bearer ${tokens[index]}`}})).status).toBe(200)
      const base=`http://127.0.0.1:${ports[index]}`
      const id=`ses_boundary_${index}_${crypto.randomUUID()}`
      const body={id,agent:"sap",location:{directory:project},model:{providerID:"sap",id:"test-only"}}
      const denied={method:"POST",headers:{"Content-Type":"application/json",Authorization:`Bearer ${tokens[1-index]}`},body:JSON.stringify(body)}
      expect((await fetch(`${base}/api/session`,denied)).status).toBe(401)
      const created=await fetch(`${base}/api/session`,{...denied,headers:{...denied.headers,Authorization:`Bearer ${tokens[index]}`}})
      expect(created.status).toBe(200)
      expect((await created.json()).data.id).toBe(id)
      expect((await fetch(`${base}/api/session/${id}/context`,{headers:{Authorization:`Bearer ${tokens[index]}`}})).status).toBe(200)
      expect((await fetch(`http://127.0.0.1:${ports[1-index]}/api/session/${id}/context`,{headers:{Authorization:`Bearer ${tokens[index]}`}})).status).toBe(401)
    }
  } finally {
    for (const process of processes) process.kill()
    await Promise.all(processes.map(process=>process.exited))
    deadEnd.stop(true)
    await rm(directory,{recursive:true,force:true})
  }
},45000)
