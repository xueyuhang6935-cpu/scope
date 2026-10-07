import { existsSync, mkdirSync, readFileSync, writeFileSync, openSync, closeSync, symlinkSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync, spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import net from 'node:net';
import pg from 'pg';
import { S3Client, CreateBucketCommand, HeadBucketCommand } from '@aws-sdk/client-s3';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const actualServices=path.join(root,'data','services');
mkdirSync(actualServices,{recursive:true});
// Windows PostgreSQL bootstrap cannot safely embed a legacy-codepage Chinese path in UTF-8 SQL.
// Junction is only an ASCII alias; physical data remains inside Scope/data/services.
const services=path.join(process.env.SystemDrive || 'C:',path.sep,'ScopeLocalServices');
if (!existsSync(services)) symlinkSync(actualServices,services,'junction');
if (realpathSync(services).toLowerCase()!==realpathSync(actualServices).toLowerCase()) throw new Error('ScopeLocalServices points at another installation');
const pgBin=path.join(services,'pgsql','bin'), pgData=path.join(services,'pgdata');
const weedDir=path.join(services,'seaweed-data');
const configPath=path.join(services,'local-storage.json');
const setup=process.argv.includes('--setup');
mkdirSync(services,{recursive:true}); mkdirSync(weedDir,{recursive:true});
if (!existsSync(configPath)) {
  if (!setup) throw new Error('Run node scripts/local-storage.mjs --setup first');
  writeFileSync(configPath,JSON.stringify({pgAdmin:randomBytes(24).toString('hex'),pgPassword:randomBytes(24).toString('hex'),
    s3AdminKey:randomBytes(12).toString('hex'),s3AdminSecret:randomBytes(24).toString('hex'),
    s3Key:randomBytes(12).toString('hex'),s3Secret:randomBytes(24).toString('hex')},null,2));
}
const config=JSON.parse(readFileSync(configPath,'utf8'));
const pgRun=(exe,args)=>execFileSync(path.join(pgBin,exe+'.exe'),args,{windowsHide:true,stdio:'ignore',timeout:60000});
if (!existsSync(path.join(pgData,'PG_VERSION'))) {
  if (!setup) throw new Error('PostgreSQL cluster not initialized');
  const pwfile=path.join(services,'pg-init-password'); writeFileSync(pwfile,config.pgAdmin);
  pgRun('initdb',['-D',pgData,'-U','postgres','--encoding=UTF8','--locale=C','--auth=scram-sha-256',`--pwfile=${pwfile}`]);
  const {unlinkSync}=await import('node:fs'); unlinkSync(pwfile);
  const conf=path.join(pgData,'postgresql.conf');
  writeFileSync(conf,readFileSync(conf,'utf8')+"\nlisten_addresses = '127.0.0.1'\nport = 55432\nmax_connections = 40\n");
}
try { pgRun('pg_ctl',['status','-D',pgData]); }
catch { pgRun('pg_ctl',['start','-D',pgData,'-l',path.join(services,'postgres.log'),'-w','-t','30']); }

async function listening(port) {
  return new Promise(resolve=>{const socket=net.connect(port,'127.0.0.1'); socket.setTimeout(500); socket.on('connect',()=>{socket.destroy();resolve(true);}); socket.on('error',()=>resolve(false)); socket.on('timeout',()=>{socket.destroy();resolve(false);});});
}
const s3Config=path.join(services,'s3-identities.json');
writeFileSync(s3Config,JSON.stringify({identities:[
  {name:'scope-admin',credentials:[{accessKey:config.s3AdminKey,secretKey:config.s3AdminSecret}],actions:['Admin','Read','Write','List','Tagging']},
  {name:'scope-app',credentials:[{accessKey:config.s3Key,secretKey:config.s3Secret}],actions:['Read:scope-artifacts','Write:scope-artifacts','List:scope-artifacts']}
]},null,2));
if (!await listening(19000)) {
  const serviceEnv=Object.fromEntries(Object.entries(process.env).filter(([key])=>key.toLowerCase()!=='debug'));
  const out=openSync(path.join(services,'seaweed.stdout.log'),'a'), err=openSync(path.join(services,'seaweed.stderr.log'),'a');
  const child=spawn(path.join(services,'seaweedfs','weed.exe'),['server',`-dir=${weedDir}`,'-ip=127.0.0.1','-ip.bind=127.0.0.1',
    '-master.port=19333','-master.telemetry=false','-master.volumeSizeLimitMB=256','-volume.port=18080','-volume.max=8',
    '-filer','-filer.port=18888','-s3','-s3.port=19000',`-s3.config=${s3Config}`,'-s3.allowDeleteBucketNotEmpty=false'],
    {cwd:weedDir,env:serviceEnv,detached:true,windowsHide:true,stdio:['ignore',out,err]});
  child.unref(); closeSync(out); closeSync(err);
  for(let i=0;i<30 && !await listening(19000);i++) await new Promise(r=>setTimeout(r,1000));
  if (!await listening(19000)) throw new Error('S3 startup failed; see data/services/seaweed.stderr.log');
}
if (setup) {
  const db=new pg.Client({host:'127.0.0.1',port:55432,user:'postgres',password:config.pgAdmin,database:'postgres'});
  await db.connect();
  try {
    if (!(await db.query("SELECT 1 FROM pg_roles WHERE rolname='scope_app'")).rowCount) {
      // Password is generated hex, never user-provided SQL.
      await db.query(`CREATE ROLE scope_app LOGIN PASSWORD '${config.pgPassword}'`);
    }
    if (!(await db.query("SELECT 1 FROM pg_database WHERE datname='scope_analyst'")).rowCount) await db.query('CREATE DATABASE scope_analyst OWNER scope_app');
  } finally { await db.end(); }
  const s3=new S3Client({endpoint:'http://127.0.0.1:19000',region:'us-east-1',forcePathStyle:true,
    credentials:{accessKeyId:config.s3AdminKey,secretAccessKey:config.s3AdminSecret},requestChecksumCalculation:'WHEN_REQUIRED'});
  try { await s3.send(new HeadBucketCommand({Bucket:'scope-artifacts'})); }
  catch(error) { if(error.$metadata?.httpStatusCode!==404) throw error; await s3.send(new CreateBucketCommand({Bucket:'scope-artifacts'})); }
  finally { s3.destroy(); }
  const envPath=path.join(root,'.env'); let env=existsSync(envPath)?readFileSync(envPath,'utf8'):'';
  const settings={ANALYST_STORAGE:'postgres',ANALYST_DATABASE_URL:`postgresql://scope_app:${config.pgPassword}@127.0.0.1:55432/scope_analyst`,
    ANALYST_S3_ENDPOINT:'http://127.0.0.1:19000',ANALYST_S3_REGION:'us-east-1',ANALYST_S3_BUCKET:'scope-artifacts',
    ANALYST_S3_ACCESS_KEY:config.s3Key,ANALYST_S3_SECRET_KEY:config.s3Secret};
  for(const [key,value] of Object.entries(settings)) {
    const line=`${key}="${value}"`, pattern=new RegExp(`^${key}=.*$`,'m');
    env=pattern.test(env)?env.replace(pattern,line):env.trimEnd()+'\n'+line+'\n';
  }
  writeFileSync(envPath,env);
}
console.log('Local PostgreSQL :55432 and private S3 :19000 are ready.');
