// Isolated Electron host: production container, preload and IPC policy, no user profile.
const { app, BaseWindow, session } = require('electron');
const path = require('node:path');
app.setPath('userData', process.env.COW_NATIVE_PROFILE);
app.whenReady().then(async () => {
    const repo = path.resolve(__dirname, '../..');
    const { createRemoteContainer } = require(path.join(repo, 'desktop/dist/main/remote/container'));
    const ipc = require(path.join(repo, 'desktop/dist/main/remote/remote-host-ipc'));
    const origin = process.env.COW_NATIVE_ORIGIN;
    const entryPaths = JSON.parse(process.env.COW_NATIVE_PATHS);
    const partition = 'parity-' + process.pid;
    const window = new BaseWindow({ width: 1300, height: 900, show: false });
    const container = createRemoteContainer({ window, partition, origin, entryPaths,
        session: session.fromPartition(partition),
        preloadPath: path.join(repo, 'desktop/dist/main/remote-preload.js'),
        openExternal: () => { throw new Error('Unexpected external navigation'); },
    });
    global.parityContainer = container;
    global.paritySignOuts = 0;
    ipc.setupRemoteHostIPC();
    ipc.registerRemoteHost({ webContents: container.webContents,
        context: () => ({ webContents: container.shellFrame, shellFrame: container.shellFrame,
            origin, entryPaths, generation: container.generation }),
        emit: payload => container.webContents.send(ipc.CHANNEL_EVENT, payload),
        signOut: async () => {
            global.paritySignOuts++;
            return { ok: true, revoked: true, message: 'isolated fixture signed out' };
        },
    });
    await container.webContents.loadURL(origin + '/knowledge/graph');
});
app.on('window-all-closed', () => app.quit());
