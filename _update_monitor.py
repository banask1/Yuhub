import os, time, subprocess, glob

UPDATE_DIR = r"C:/Users/M1racle/AppData/Local/Yuhub/update"
TARGET = r"D:/TOOL/Yuhub/Yuhub.exe"
TEMP = r"C:/Users/M1racle/AppData/Local/Temp"
LOG = r"C:/Users/M1racle/Desktop/Yuhub/_update_monitor.log"

def procs():
    try:
        out = subprocess.run(["tasklist","/FI","IMAGENAME eq Yuhub.exe","/FO","CSV","/NH"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        pids=[]
        for line in out.splitlines():
            if "Yuhub.exe" in line:
                parts=[p.strip('"') for p in line.split('","')]
                if len(parts)>=2: pids.append(parts[1])
        return pids
    except Exception as e:
        return ["ERR:"+str(e)]

def snap():
    rec={}
    rec["pids"]=procs()
    rec["upd"]=[]
    if os.path.isdir(UPDATE_DIR):
        for n in sorted(os.listdir(UPDATE_DIR)):
            p=os.path.join(UPDATE_DIR,n)
            if os.path.isfile(p): rec["upd"].append("%s:%d"%(n,os.path.getsize(p)))
    rl=os.path.join(UPDATE_DIR,"replace.log")
    if os.path.exists(rl):
        try:
            rec["logtail"]=open(rl,encoding="utf-8",errors="replace").read().splitlines()[-5:]
        except: rec["logtail"]=[]
    else:
        rec["logtail"]=["(无 replace.log)"]
    if os.path.exists(TARGET):
        st=os.stat(TARGET); rec["target"]="%d @ %s"%(st.st_size, time.strftime("%H:%M:%S",time.localtime(st.st_mtime)))
    else:
        rec["target"]="(目标不存在!)"
    acks=[]
    for d in glob.glob(os.path.join(TEMP,"Yuhub_upd_*")):
        if os.path.exists(os.path.join(d,"ack")): acks.append(os.path.basename(d))
        if os.path.exists(d+".ack"): acks.append(os.path.basename(d)+".ack")
    rec["ack"]=acks
    return rec

start=time.time()
with open(LOG,"w",encoding="utf-8") as f:
    f.write("开始监控 @ %s\n"%time.strftime("%Y-%m-%d %H:%M:%S"))
    while time.time()-start < 150:
        r=snap()
        f.write("[%s] pids=%s | target=%s | ack=%s\n"%(time.strftime("%H:%M:%S"),r["pids"],r["target"],r["ack"]))
        f.write("    upd=%s\n"%r["upd"])
        f.write("    log: "+(" || ".join(r["logtail"]))+"\n")
        f.flush()
        time.sleep(2)
    f.write("监控结束 @ %s\n"%time.strftime("%H:%M:%S"))
print("monitor done ->", LOG)
