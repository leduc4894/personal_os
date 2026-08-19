import { execFile } from "node:child_process";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const runFile = promisify(execFile);

export async function runFromE2eRepositoryRoot(
  executable: string,
  arguments_: readonly string[],
  specModuleUrl: string,
  environment: NodeJS.ProcessEnv = process.env,
): Promise<{ readonly stdout: string; readonly stderr: string }> {
  const repositoryRoot = path.resolve(
    path.dirname(fileURLToPath(specModuleUrl)),
    "../../../..",
  );
  return await runFile(executable, [...arguments_], {
    cwd: repositoryRoot,
    env: environment,
  });
}
