# Troubleshooting: Codelet Deployment in Kubernetes Environment

## Problem Summary

When deploying JRTC applications in Kubernetes using `jrtc-ctl load`, you may encounter a "400 Bad Request" error followed by connection failures to the JBPF agent. This document provides the root cause analysis and solution.

## Error Symptoms

```bash
INFO[0000] loaded app                                    id=6 startTime="2025-08-13 03:26:27.916256582 +0000 UTC"
TRAC[0000] sending http request                          method=POST url="http://127.0.0.1:30450"
Error: Post "http://127.0.0.1:30450": dial tcp 127.0.0.1:30450: connect: connection refused
```

## Root Cause

The `jrtc-ctl` tool is hardcoded to use `127.0.0.1` for JBPF device connections, but in Kubernetes environments, the reverse proxy is accessible via service names, not localhost.

## Architecture Overview

### Container Deployment
The `srs-gnb-du1-0` pod contains three containers:
- **`troubleshooter`**: Debugging container
- **`gnb`**: Main gNodeB application
- **`srs-gnb-proxy`**: Reverse proxy for JBPF communication

### Service Endpoints
- JRTC Service: `jrtc-service.ran.svc.cluster.local:3001`
- JBPF Reverse Proxy: `srs-gnb-du1-proxy.ran.svc.cluster.local:30450`
- JRTC Decoder: `jrtc-decoder.ran.svc.cluster.local:20789`

## Solution

### Method 1: Manual Codelet Deployment (Recommended)

When `jrtc-ctl load` fails at the codelet deployment step, you can manually deploy the codelets:

#### Step 1: Prepare the Codelet Payload
```bash
kubectl exec -it jrtc-0 -n ran -c jrtc -- bash -c 'cat > /tmp/codelet_payload.json << "EOF"
{
  "codelet_descriptor": [
    {
      "codelet_path": "/codelets/xran_packets/xran_packets_collect.o",
      "hook_name": "capture_xran_packet",
      "codelet_name": "collector",
      "priority": 1
    },
    {
      "codelet_path": "/codelets/xran_packets/xran_packets_report.o",
      "hook_name": "report_stats",
      "linked_maps": [
        {
          "linked_codelet_name": "collector",
          "linked_map_name": "output_tmp_map",
          "map_name": "output_tmp_map"
        }
      ],
      "codelet_name": "reporter",
      "out_io_channel": [
        {
          "name": "output_map",
          "stream_id": "000813d82e92aa151cfa732f0a2f6ec2",
          "serde": {
            "file_path": "/codelets/xran_packets/xran_packet_info:packet_stats_serializer.so"
          }
        }
      ],
      "priority": 2
    }
  ],
  "codeletset_id": "xran_packets"
}
EOF'
```

#### Step 2: Deploy to Reverse Proxy
```bash
kubectl exec -it jrtc-0 -n ran -c jrtc -- curl -X POST \
  -H "Content-Type: application/json" \
  -v http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450 \
  --data @/tmp/codelet_payload.json
```

#### Expected Success Response
```
< HTTP/1.1 201 Created
< Server: 0.1.0-
< Content-Length: 0
```

### Method 2: Fix Deployment Configuration

Update your `deployment_fixed.yaml` to include the correct host for JBPF devices:

```yaml
jbpf:
  device:
    - id: 1
      host: srs-gnb-du1-proxy.ran.svc.cluster.local
      port: 30450
```

**Note**: This may not work due to `jrtc-ctl` hardcoded defaults, but it's worth trying.

## Complete Deployment Process

### 1. Load the Application
```bash
kubectl exec -it jrtc-0 -n ran -c jrtc -- bash -c '
export JRTC_APPS=/apps
export JBPF_CODELETS=/codelets
/jrtc/out/bin/jrtc-ctl load -c /apps/xran_packets/deployment_fixed.yaml --log-level trace
'
```

### 2. If Codelet Deployment Fails
Follow the manual deployment steps above.

### 3. Verify Deployment
```bash
# Check app is loaded
kubectl exec -it jrtc-0 -n ran -c jrtc -- curl -s http://127.0.0.1:3001/app | grep -o '"app_name":"[^"]*"'

# Check JRTC logs
kubectl logs jrtc-0 -n ran -c jrtc --tail=10
```

## Troubleshooting Commands

### Check Pod Status
```bash
kubectl get pods -n ran
kubectl get services -n ran
```

### Check Container Logs
```bash
# JRTC Controller logs
kubectl logs jrtc-0 -n ran -c jrtc --tail=20

# JRTC Decoder logs  
kubectl logs jrtc-0 -n ran -c jrtc-decoder --tail=20

# gnb logs
kubectl logs srs-gnb-du1-0 -n ran -c gnb --tail=20

# Reverse proxy logs
kubectl logs srs-gnb-du1-0 -n ran -c srs-gnb-proxy --tail=20
```

### Test Connectivity
```bash
# Test JRTC service
kubectl exec -it jrtc-0 -n ran -c jrtc -- curl -v http://127.0.0.1:3001/app

# Test reverse proxy
kubectl exec -it jrtc-0 -n ran -c jrtc -- curl -v http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450/
```

### Unload Application
```bash
kubectl exec -it jrtc-0 -n ran -c jrtc -- bash -c '
export JRTC_APPS=/apps
export JBPF_CODELETS=/codelets
/jrtc/out/bin/jrtc-ctl unload -c /apps/xran_packets/deployment_fixed.yaml --log-level trace
'
```

## Success Indicators

1. **App Loaded**: `INFO[0000] loaded app id=X startTime="..."`
2. **Schema Loaded**: `INFO[0000] successfully upserted proto package`
3. **Stream Associated**: `INFO[0000] successfully associated stream ID`
4. **Codelets Deployed**: `HTTP/1.1 201 Created` from reverse proxy
5. **App Running**: App appears in `curl http://127.0.0.1:3001/app`

## Common Issues

### "400 Bad Request" on Load
- **Cause**: App already loaded
- **Solution**: Unload first, then reload

### "Connection Refused" to 127.0.0.1:30450
- **Cause**: Hardcoded localhost in `jrtc-ctl`
- **Solution**: Use manual codelet deployment method

### "Empty Reply from Server" 
- **Cause**: Wrong endpoint or payload format
- **Solution**: Verify service name and JSON payload

## Environment Variables

Required environment variables for `jrtc-ctl`:
```bash
export JRTC_APPS=/apps
export JBPF_CODELETS=/codelets
```

## Files Involved

- `/apps/xran_packets/deployment_fixed.yaml` - Deployment configuration
- `/codelets/xran_packets/` - Codelet binaries and configurations
- `/apps/xran_packets/xran_packets.py` - Python application

## Related Documentation

- [README.md](../README.md) - Main deployment guide
- [jrtc-apps/README.md](./README.md) - JRTC applications documentation

export KUBECONFIG="$(k3d kubeconfig write janus-cluster)" && kubectl exec -it jrtc-0 -n ran -c jrtc -- curl -X DELETE -v http://srs-gnb-du1-proxy.ran.svc.cluster.local:30450/xran_packets