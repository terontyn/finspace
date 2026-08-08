const { spawn } = require("node:child_process");

const exampleKey = "change_me_with_long_random_value";
const encryptionKey = process.env.N8N_ENCRYPTION_KEY;

if (!encryptionKey || encryptionKey === exampleKey) {
  console.error("N8N_ENCRYPTION_KEY must be set to a private random value");
  process.exit(78);
}

const child = spawn("n8n", ["start"], {
  env: process.env,
  stdio: "inherit",
});

const forwardedSignals = ["SIGINT", "SIGTERM"];

for (const signal of forwardedSignals) {
  process.on(signal, () => child.kill(signal));
}

child.on("error", () => {
  console.error("Unable to start n8n");
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    for (const forwardedSignal of forwardedSignals) {
      process.removeAllListeners(forwardedSignal);
    }
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
