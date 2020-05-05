const core = require('@actions/core');
const proc = require('child_process');

try {
        const arg_image = String(core.getInput('image'));
        const arg_run = String(core.getInput('run'));

        arg_cwd = process.cwd()

        console.log('Pull CI Image');
        console.log('--------------------------------------------------------------------------------');
        proc.execFileSync(
                '/usr/bin/docker',
                [
                        'pull',
                        '--quiet',
                        arg_image
                ],
                {
                        stdio: 'inherit'
                }
        );
        console.log('--------------------------------------------------------------------------------');
        console.log('Execute CI');
        console.log('--------------------------------------------------------------------------------');
        proc.execFileSync(
                '/usr/bin/docker',
                [
                        'run',
                                '--privileged',
                                '--rm',
                                '--volume=' + arg_cwd + ':/ci/workdir',
                                arg_image,
                                '/bin/bash',
                                        '-c',
                                        arg_run
                ],
                {
                        stdio: 'inherit'
                }
        );
        console.log('--------------------------------------------------------------------------------');
        console.log(`End of CI`);
} catch (error) {
        core.setFailed(error.message);
}
