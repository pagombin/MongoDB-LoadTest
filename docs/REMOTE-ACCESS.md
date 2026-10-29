# Opening the UI from your laptop through a jump server

Ballast runs on a Linux server and serves its UI over HTTPS. Ballast connects to your MongoDB or Atlas cluster from that server, so your laptop only has to reach the UI, never the cluster.

This guide covers the common setup where:
- the server can only be reached through a jump server (bastion);
- you log in as your own username, then switch to the service account with `sudo su - mongod`.

Pick the first route that works for you.

| Route | Ballast listens on | Needs |
|---|---|---|
| A. Tunnel through the jump server | `127.0.0.1:8443` (the default) | SSH port forwarding allowed on the jump server and on the Ballast server |
| B. Two separate hops | `127.0.0.1:8443` | Same as A; use it when `-J` does not work for you |
| C. Ballast on the server's network, tunnel to the jump server only | `0.0.0.0:8443`, with an access token | Forwarding allowed on the jump server only, and port 8443 open from the jump server to the Ballast server |

Throughout, replace:
- `myuser` with your own username;
- `JUMP_SERVER` with the jump server's address;
- `lxv105350` with the Ballast server's host name;
- `/apps/mongod/MongoDB-LoadTest` with your Ballast directory.

The commands work in PowerShell on Windows 10 or 11 (OpenSSH is built in), in Terminal on macOS, and in a Linux shell.

## Who you log in as does not matter

A tunnel is tied to the machine, not to the user. Ballast running as `mongod` listens on `127.0.0.1:8443`, and an SSH login to that machine as `myuser` can forward to that address. Open the tunnel as yourself, then use `sudo su - mongod` inside the session as usual to start or stop Ballast.

## A. Tunnel through the jump server

From your laptop:

```
ssh -4 -J myuser@JUMP_SERVER -L 8443:127.0.0.1:8443 myuser@lxv105350
```

In that session:

```
sudo su - mongod
cd /apps/mongod/MongoDB-LoadTest
bin/ballastctl start
```

On your laptop, open `https://localhost:8443`. `localhost` here is your laptop's end of the tunnel. Compare the certificate fingerprint with the `certificate SHA-256` line from `bin/ballastctl start`, then accept the certificate.

Keep the SSH window open while you use the UI; closing it closes the tunnel. Ballast keeps running on the server.

If port 8443 is already in use on your laptop, use another local port, for example `-L 9443:127.0.0.1:8443`, and open `https://localhost:9443`.

## B. Two separate hops

Use this when `-J` fails. That happens, for example, when your key or SSH settings for the Ballast server exist only on the jump server.

```
# on your laptop
ssh -4 -L 8443:127.0.0.1:18443 myuser@JUMP_SERVER

# then, on the jump server
ssh -4 -L 18443:127.0.0.1:8443 myuser@lxv105350

# then, on lxv105350
sudo su - mongod
cd /apps/mongod/MongoDB-LoadTest
bin/ballastctl start
```

Open `https://localhost:8443` on your laptop. Port 18443 is only a temporary port on the jump server; any free port works if you change it in both commands.

To check the second hop, open another session on the jump server and run:

```
curl -k -s -o /dev/null -w "%{http_code}\n" https://127.0.0.1:18443/api/about
```

`200` means the hop works. See "Messages and what they mean" below if it does not.

## C. Ballast on the server's network

Use this when SSH port forwarding is turned off on the Ballast server. The symptom is `channel 3: open failed: administratively prohibited: open failed` in your session on that server. With this route, Ballast listens on the server's network address, and the jump server connects to it directly. Only the laptop → jump server hop uses SSH forwarding.

### 1. On the Ballast server, as `mongod`

```
cd /apps/mongod/MongoDB-LoadTest
head -c 24 /dev/urandom | base64 > config/token
chmod 600 config/token
cat > config/ballast.yaml <<'EOF'
server:
  listen: 0.0.0.0:8443
  access_token_file: config/token
EOF
bin/ballastctl restart
cat config/token
```

The access token is required. Once Ballast listens on the network, anyone who can reach port 8443 could otherwise use it to start load against your clusters.

If `config/ballast.yaml` already exists, add the two `server:` keys to it instead of overwriting it.

To listen on one address only, use it in place of `0.0.0.0`, for example `listen: 10.20.30.40:8443`.

### 2. On the jump server, check that it can reach Ballast

```
curl -k -s -o /dev/null -w "%{http_code}\n" https://lxv105350:8443/api/about
```

- `401` means it is reachable; Ballast is asking for the token. This is the expected answer.
- `200` also means it is reachable.
- `000`, after a pause, means a firewall blocks port 8443 between the jump server and the Ballast server. Opening it needs the server administrators (for example `firewall-cmd --add-port=8443/tcp` as root), because `mongod` cannot change the firewall.

### 3. Close any earlier tunnel windows, then on your laptop

```
ssh -4 -L 8443:lxv105350:8443 myuser@JUMP_SERVER
```

This is a single hop: the jump server itself connects to `lxv105350:8443`.

### 4. Open `https://localhost:8443` on your laptop

Accept the certificate, then paste the token from step 1. The browser keeps a session cookie, so you enter the token once per browser session.

### Going back to route A or B

Remove the `server:` lines from `config/ballast.yaml`, or set `listen: 127.0.0.1:8443`, and run `bin/ballastctl restart`.

## PuTTY and MobaXterm

- **PuTTY 0.77 or later:**
  - Under Connection → Proxy, set the proxy type to **SSH** and enter the jump server's address.
  - Under Connection → SSH → Tunnels, enter source port `8443` and destination `127.0.0.1:8443`, then click Add.
  - Connect to the Ballast server.
  - For route C, skip the proxy, connect to the jump server, and use destination `lxv105350:8443`.
- **MobaXterm:**
  - In the SSH session's Network settings, set **SSH gateway (jump host)** to the jump server.
  - Add the port forward with the Tunneling tool, using the same ports as above.

## Messages and what they mean

| Message | Where | Meaning and fix |
|---|---|---|
| `bind [::1]:18443: Cannot assign requested address` | Any hop | Harmless. IPv6 is off on that machine, so ssh could only listen on IPv4, which is the address the tunnel uses. Add `-4` to the ssh command to hide it. |
| `bind [127.0.0.1]:8443: Address already in use` | Any hop | Something else uses that port on that machine. Pick another local port number. |
| `channel N: open failed: administratively prohibited: open failed` | In your session on the machine that refuses | That machine's SSH server does not allow port forwarding. If it appears on the Ballast server, use route C. If it appears on the jump server, ask its administrators to allow forwarding, or open the UI from a browser on the jump server, if it has one, at `https://lxv105350:8443` after route C step 1. |
| `channel N: open failed: connect failed: Connection refused` | Any hop | The tunnel works, but nothing listens at the destination. Check `bin/ballastctl status` as `mongod`, and that the port in the tunnel matches `server.listen`. |
| The browser shows a certificate warning | Laptop | Expected for the self-signed certificate. Compare the fingerprint with `bin/ballastctl start` (or `bin/ballastctl status`), then accept it. |
| The browser asks for an access token | Laptop | `server.access_token_file` is set. Paste the contents of `config/token` from the Ballast server. |

## Security notes

- Other users logged in to the Ballast server can also reach `127.0.0.1:8443`. Setting an access token, as in route C, protects against that on routes A and B too.
- Route C opens a port on the server's network. Agree it with whoever runs the server and the network, especially where port forwarding was disabled on purpose.
