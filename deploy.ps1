param(
  [string]$TerraformDir = $PSScriptRoot,
  [string]$KeyPath = "$PSScriptRoot\keys\id_ed25519",
  [string]$NodePassword = $env:K8S_NODE_PASSWORD,
  [int]$NodePort = 30080
)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

if (-not $NodePassword) {
  throw "Missing node password. Set K8S_NODE_PASSWORD or pass -NodePassword."
}
$tf = $env:TERRAFORM_PATH
if (-not $tf) {
  $cmd = Get-Command terraform -ErrorAction SilentlyContinue
  if ($cmd) { $tf = $cmd.Source }
}
if (-not $tf) {
  $localTf = Join-Path $PSScriptRoot "terraform.exe"
  if (Test-Path $localTf) { $tf = $localTf }
}
if (-not $tf) {
  throw "terraform not found. Set TERRAFORM_PATH or install it in PATH."
}

# 1. Read terraform outputs
Push-Location $TerraformDir
$masterPublic = (& $tf output -raw master1_public_ip).Trim()
$masterPriv = ((& $tf output -json master_private_ips) -replace '[\[\]\s"]', '') -split ','
$workerPriv = ((& $tf output -json worker_private_ips) -replace '[\[\]\s"]', '') -split ','
$apiEndpoint = $masterPriv[0]
$apiOut = & $tf output -raw api_endpoint 2>$null
if ($LASTEXITCODE -eq 0 -and $apiOut) { $apiEndpoint = $apiOut.Trim() }
Pop-Location
Write-Host ">>> master1 public IP: $masterPublic"
Write-Host ">>> API endpoint: $apiEndpoint"

# 2. Generate Kubekey cluster config (kubeadm HA + VIP)
$hosts = @(); $etcd = @(); $cp = @(); $wk = @()
for ($i = 0; $i -lt $masterPriv.Count; $i++) {
  $n = "master$($i + 1)"
  $hosts += "    - {name: $n, address: $($masterPriv[$i]), internalAddress: $($masterPriv[$i]), user: root, password: `"$NodePassword`"}"
  $etcd += "      - $n"
  $cp += "      - $n"
}
for ($i = 0; $i -lt $workerPriv.Count; $i++) {
  $n = "worker$($i + 1)"
  $hosts += "    - {name: $n, address: $($workerPriv[$i]), internalAddress: $($workerPriv[$i]), user: root, password: `"$NodePassword`"}"
  $wk += "      - $n"
}
$config = @"
apiVersion: kubekey.kubesphere.io/v1alpha2
kind: Cluster
metadata:
  name: course-k8s
spec:
  hosts:
$($hosts -join "`n")
  roleGroups:
    etcd:
$($etcd -join "`n")
    control-plane:
$($cp -join "`n")
    worker:
$($wk -join "`n")
  controlPlaneEndpoint:
    domain: lb.kubesphere.local
    address: "$apiEndpoint"
    port: 6443
  kubernetes:
    version: v1.28.2
    clusterName: course-k8s
    autoRenewCerts: true
  etcd:
    type: kubekey
  network:
    plugin: calico
    kubePodsCIDR: 10.233.64.0/18
    kubeServiceCIDR: 10.233.0.0/18
  registry:
    privateRegistry: ""
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText("$TerraformDir\kubekey-cluster.yaml", $config, $utf8NoBom)

# 3. Package application
$tar = "$TerraformDir\course-selection.tar.gz"
Push-Location "$TerraformDir\course-selection"
if (Test-Path $tar) { Remove-Item $tar -Force }
tar -czf $tar app k8s loadtest vm2k8s.py docker-compose.yml
Pop-Location

# 4. Wait for SSH
$sshArgs = @("-i", $KeyPath, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL", "-o", "ConnectTimeout=10")
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  & ssh @sshArgs "root@$masterPublic" "echo ok" 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { $ok = $true; break }
  Start-Sleep -Seconds 10
}
if (-not $ok) { throw "SSH connection failed (check instance/security group)" }

# 5. Upload
& scp @sshArgs "$TerraformDir\kubekey-cluster.yaml" "$tar" "$TerraformDir\scripts\install-kk.sh" "$TerraformDir\scripts\deploy-app.sh" "root@${masterPublic}:/root/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }
& ssh @sshArgs "root@$masterPublic" "mkdir -p /root/course-selection && tar -xzf /root/course-selection.tar.gz -C /root/course-selection && chmod +x /root/install-kk.sh /root/deploy-app.sh"
if ($LASTEXITCODE -ne 0) { throw "upload/extract failed" }

# 6. Install Kubekey and create standard K8s HA cluster
& ssh @sshArgs "root@$masterPublic" "bash /root/install-kk.sh"
if ($LASTEXITCODE -ne 0) { throw "Kubekey install failed (check get-kk network)" }

& ssh @sshArgs "root@$masterPublic" "echo yes | /root/kk create cluster -f /root/kubekey-cluster.yaml"
if ($LASTEXITCODE -ne 0) { throw "Cluster creation failed (check /root/kubekey.log)" }

# 7. Deploy course-selection app
$nodeIps = (($masterPriv + $workerPriv) -join " ")
& ssh @sshArgs "root@$masterPublic" "NODE_PASSWORD='$NodePassword' MASTER1_IP='$($masterPriv[0])' NODE_IPS='$nodeIps' NODE_PORT='$NodePort' bash /root/deploy-app.sh"
if ($LASTEXITCODE -ne 0) { throw "App deployment failed" }

# 8. Verify + load test from Windows
Start-Sleep -Seconds 3
$baseUrl = "http://${masterPublic}:$NodePort"
curl.exe -s "$baseUrl/health"
Write-Host ""
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$loadTest = "$TerraformDir\course-selection\loadtest\load_test.py"
if ($py -and (Test-Path $loadTest)) {
  & $py $loadTest --base $baseUrl --course 7 --students 1000 --workers 50
}

Write-Host ""
Write-Host "Deployed. Browser: $baseUrl"
Write-Host "Destroy: cd $TerraformDir; & '$tf' destroy -auto-approve"
