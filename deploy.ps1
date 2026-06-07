#!/usr/bin/env pwsh
<#
.SYNOPSIS
    RAKTA-SETU — One-shot AWS deployment script (us-east-1 / N. Virginia)
.DESCRIPTION
    Run this script once to set everything up. After the first run, re-run
    with -DeployOnly to just push a new image + frontend build.
.PARAMETER AccountId
    Your 12-digit AWS account ID (find it at: aws sts get-caller-identity)
.PARAMETER AppRunnerUrl
    ONLY needed on re-runs after first deploy. The App Runner URL from step 2.
.PARAMETER DeployOnly
    Skip infra creation, just rebuild + push the Docker image and rebuild the frontend.
.EXAMPLE
    # First time setup:
    .\deploy.ps1 -AccountId 123456789012

    # After you have the App Runner URL (step 3 output):
    .\deploy.ps1 -AccountId 123456789012 -AppRunnerUrl "https://abcd1234.us-east-1.awsapprunner.com"

    # Quick redeploy (code change):
    .\deploy.ps1 -AccountId 123456789012 -AppRunnerUrl "https://abcd1234.us-east-1.awsapprunner.com" -DeployOnly
#>

param(
    [Parameter(Mandatory=$true)]
    [string]$AccountId,

    [string]$AppRunnerUrl = "",

    [switch]$DeployOnly
)

$ErrorActionPreference = "Stop"
$REGION      = "us-east-1"
$REPO_NAME   = "rakta-setu-api"
$SERVICE_NAME = "rakta-setu-api"
$IAM_ROLE    = "RaktaSetuAppRunnerRole"
$BUCKET_NAME = "rakta-setu-frontend-$AccountId"   # must be globally unique
$SECRET_NAME = "rakta-setu/prod"
$ECR_URI     = "$AccountId.dkr.ecr.$REGION.amazonaws.com/$REPO_NAME"

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  RAKTA-SETU AWS Deploy  |  Region: $REGION" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# ── 0. Verify prerequisites ────────────────────────────────────────────────
Write-Host "[0/7] Checking prerequisites..." -ForegroundColor Yellow

if (!(Get-Command aws -ErrorAction SilentlyContinue))  { throw "AWS CLI not found. Install from: https://aws.amazon.com/cli/" }
if (!(Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker not found. Install Docker Desktop first." }
if (!(Get-Command npm -ErrorAction SilentlyContinue))  { throw "npm not found. Install Node.js first." }

$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
Write-Host "  AWS identity: $($identity.Arn)" -ForegroundColor Green

if ($DeployOnly) {
    Write-Host "  -DeployOnly flag set — skipping infra, going straight to build+push." -ForegroundColor Cyan
} else {
    # ── 1. Create ECR Repository ───────────────────────────────────────────────
    Write-Host ""
    Write-Host "[1/7] Creating ECR repository..." -ForegroundColor Yellow
    $existingRepo = aws ecr describe-repositories --repository-names $REPO_NAME --region $REGION 2>$null
    if ($existingRepo) {
        Write-Host "  ECR repo already exists — skipping." -ForegroundColor Green
    } else {
        aws ecr create-repository --repository-name $REPO_NAME --region $REGION | Out-Null
        Write-Host "  Created ECR repo: $ECR_URI" -ForegroundColor Green
    }

    # ── 2. Create IAM Role for App Runner ─────────────────────────────────────
    Write-Host ""
    Write-Host "[2/7] Setting up IAM role..." -ForegroundColor Yellow

    $trustPolicy = @"
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": ["tasks.apprunner.amazonaws.com", "build.apprunner.amazonaws.com"]},
    "Action": "sts:AssumeRole"
  }]
}
"@
    $trustFile = "$env:TEMP\rakta-trust.json"
    $trustPolicy | Out-File -FilePath $trustFile -Encoding utf8

    $existingRole = aws iam get-role --role-name $IAM_ROLE 2>$null
    if ($existingRole) {
        Write-Host "  IAM role already exists — skipping creation." -ForegroundColor Green
    } else {
        aws iam create-role --role-name $IAM_ROLE `
            --assume-role-policy-document file://$trustFile | Out-Null

        $policies = @(
            "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
            "arn:aws:iam::aws:policy/TranslateFullAccess",
            "arn:aws:iam::aws:policy/AmazonPollyFullAccess",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            "arn:aws:iam::aws:policy/SecretsManagerReadWrite",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
        )
        foreach ($p in $policies) {
            aws iam attach-role-policy --role-name $IAM_ROLE --policy-arn $p
            Write-Host "  Attached: $p" -ForegroundColor Green
        }
    }
    $ROLE_ARN = (aws iam get-role --role-name $IAM_ROLE --output json | ConvertFrom-Json).Role.Arn
    Write-Host "  Role ARN: $ROLE_ARN" -ForegroundColor Green

    # ── 3. Store secrets in Secrets Manager ───────────────────────────────────
    Write-Host ""
    Write-Host "[3/7] Secrets Manager setup..." -ForegroundColor Yellow

    $existingSecret = aws secretsmanager describe-secret --secret-id $SECRET_NAME --region $REGION 2>$null
    if (!$existingSecret) {
        Write-Host ""
        Write-Host "  Enter your Twilio + ElevenLabs secrets (leave blank to set later in AWS Console):" -ForegroundColor Cyan
        $twilio_sid   = Read-Host "  TWILIO_ACCOUNT_SID (AC...)"
        $twilio_token = Read-Host "  TWILIO_AUTH_TOKEN"
        $twilio_from  = Read-Host "  TWILIO_CALL_FROM (+1...)"
        $elevenlabs   = Read-Host "  ELEVENLABS_API_KEY (optional)"
        $donor_ph     = Read-Host "  DONOR_PHONE (+91...)"
        $patient_ph   = Read-Host "  PATIENT_PHONE (+91..., default +917416470528)"
        if (!$patient_ph) { $patient_ph = "+917416470528" }

        $secretValue = @{
            TWILIO_ACCOUNT_SID = $twilio_sid
            TWILIO_AUTH_TOKEN  = $twilio_token
            TWILIO_CALL_FROM   = $twilio_from
            ELEVENLABS_API_KEY = $elevenlabs
            PUBLIC_BASE_URL    = "PENDING_AFTER_APPRUNNER_DEPLOY"
            DONOR_PHONE        = $donor_ph
            PATIENT_PHONE      = $patient_ph
        } | ConvertTo-Json -Compress

        aws secretsmanager create-secret `
            --name $SECRET_NAME `
            --region $REGION `
            --secret-string $secretValue | Out-Null
        Write-Host "  Secret created: $SECRET_NAME" -ForegroundColor Green
    } else {
        Write-Host "  Secret already exists — skipping (update manually in AWS Console if needed)." -ForegroundColor Green
    }
}

# ── 4. Build + Push Docker Image ──────────────────────────────────────────
Write-Host ""
Write-Host "[4/7] Building + pushing Docker image to ECR..." -ForegroundColor Yellow

$PROJECT_ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $PROJECT_ROOT

# ECR Login
Write-Host "  Authenticating Docker with ECR..."
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin "$AccountId.dkr.ecr.$REGION.amazonaws.com"

# Build
Write-Host "  Building image (this takes ~3-5 minutes the first time)..."
docker build -t $REPO_NAME .

# Tag + Push
docker tag "${REPO_NAME}:latest" "${ECR_URI}:latest"
Write-Host "  Pushing to ECR..."
docker push "${ECR_URI}:latest"
Write-Host "  Docker image pushed: ${ECR_URI}:latest" -ForegroundColor Green

# ── 5. Create / Update App Runner Service ─────────────────────────────────
Write-Host ""
Write-Host "[5/7] Deploying to AWS App Runner..." -ForegroundColor Yellow

$serviceExists = aws apprunner list-services --region $REGION --output json | ConvertFrom-Json | 
    Select-Object -ExpandProperty ServiceSummaryList | 
    Where-Object { $_.ServiceName -eq $SERVICE_NAME }

if (!$serviceExists) {
    Write-Host "  Creating App Runner service (takes ~3-5 minutes)..." -ForegroundColor Cyan

    $serviceConfig = @{
        ServiceName = $SERVICE_NAME
        SourceConfiguration = @{
            ImageRepository = @{
                ImageIdentifier     = "${ECR_URI}:latest"
                ImageRepositoryType = "ECR"
                ImageConfiguration  = @{
                    Port = "8080"
                    RuntimeEnvironmentVariables = @{
                        PYTHONUNBUFFERED     = "1"
                        AWS_DEFAULT_REGION   = $REGION
                        CALL_ENABLED         = "true"
                        ESCALATE_AFTER_SECS  = "20"
                    }
                }
            }
            AutoDeploymentsEnabled = $true
        }
        InstanceConfiguration = @{
            Cpu    = "1 vCPU"
            Memory = "2 GB"
        }
        HealthCheckConfiguration = @{
            Path     = "/health"
            Protocol = "HTTP"
        }
        InstanceRoleArn = $ROLE_ARN
    } | ConvertTo-Json -Depth 10 -Compress

    $configFile = "$env:TEMP\rakta-apprunner-config.json"
    $serviceConfig | Out-File -FilePath $configFile -Encoding utf8

    $svc = aws apprunner create-service --cli-input-json file://$configFile --region $REGION --output json | ConvertFrom-Json
    $SERVICE_ARN = $svc.Service.ServiceArn
    Write-Host "  Service ARN: $SERVICE_ARN" -ForegroundColor Green

    # Wait for it to become running
    Write-Host "  Waiting for App Runner to spin up (polling every 30s)..." -ForegroundColor Cyan
    $attempts = 0
    do {
        Start-Sleep -Seconds 30
        $status = (aws apprunner describe-service --service-arn $SERVICE_ARN --region $REGION --output json | ConvertFrom-Json).Service.Status
        Write-Host "  Status: $status"
        $attempts++
    } while ($status -ne "RUNNING" -and $attempts -lt 20)

    if ($status -eq "RUNNING") {
        $AppRunnerUrl = (aws apprunner describe-service --service-arn $SERVICE_ARN --region $REGION --output json | ConvertFrom-Json).Service.ServiceUrl
        $AppRunnerUrl = "https://$AppRunnerUrl"
        Write-Host ""
        Write-Host "  ✅ App Runner is LIVE: $AppRunnerUrl" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️  App Runner not yet running after 10 min. Check AWS Console for details." -ForegroundColor Yellow
        Write-Host "     Run this script again with -AppRunnerUrl once it's running." -ForegroundColor Yellow
    }
} else {
    Write-Host "  App Runner service already exists." -ForegroundColor Green
    if (!$AppRunnerUrl) {
        $svcDetails = aws apprunner list-services --region $REGION --output json | ConvertFrom-Json | 
            Select-Object -ExpandProperty ServiceSummaryList | 
            Where-Object { $_.ServiceName -eq $SERVICE_NAME }
        $AppRunnerUrl = "https://$($svcDetails.ServiceUrl)"
    }
    Write-Host "  Triggering new deployment (image updated)..."
    $SERVICE_ARN = ($serviceExists | Select-Object -First 1).ServiceArn
    aws apprunner start-deployment --service-arn $SERVICE_ARN --region $REGION | Out-Null
    Write-Host "  Deployment started. New image will be live in ~3 minutes." -ForegroundColor Green
}

Write-Host ""
Write-Host "  App Runner URL: $AppRunnerUrl" -ForegroundColor Cyan

# Update SECRET with real PUBLIC_BASE_URL if we have it
if ($AppRunnerUrl -and $AppRunnerUrl -ne "") {
    Write-Host "  Updating PUBLIC_BASE_URL in Secrets Manager..."
    $currentSecret = aws secretsmanager get-secret-value --secret-id $SECRET_NAME --region $REGION --output json | ConvertFrom-Json
    $secretObj = $currentSecret.SecretString | ConvertFrom-Json
    $secretObj.PUBLIC_BASE_URL = $AppRunnerUrl
    $updatedSecret = $secretObj | ConvertTo-Json -Compress
    aws secretsmanager update-secret --secret-id $SECRET_NAME --region $REGION --secret-string $updatedSecret | Out-Null
    Write-Host "  PUBLIC_BASE_URL updated in Secrets Manager." -ForegroundColor Green
}

# ── 6. Build + Deploy Frontend to S3 + CloudFront ─────────────────────────
Write-Host ""
Write-Host "[6/7] Building + deploying frontend..." -ForegroundColor Yellow

if (!$AppRunnerUrl) {
    Write-Host "  ⚠️  No App Runner URL yet — skipping frontend build." -ForegroundColor Yellow
    Write-Host "     Re-run with -AppRunnerUrl https://YOUR-URL after App Runner is live." -ForegroundColor Yellow
} else {
    Set-Location "$PROJECT_ROOT\frontend"

    # Build with the backend URL baked in
    $env:VITE_API_URL = $AppRunnerUrl
    Write-Host "  Building frontend with VITE_API_URL=$AppRunnerUrl"
    npm ci --silent
    npm run build

    # Create S3 bucket if needed
    $bucketExists = aws s3 ls "s3://$BUCKET_NAME" 2>$null
    if (!$bucketExists) {
        Write-Host "  Creating S3 bucket: $BUCKET_NAME"
        # us-east-1 does NOT use --create-bucket-configuration
        aws s3api create-bucket --bucket $BUCKET_NAME --region $REGION | Out-Null

        # Enable static website hosting
        aws s3 website "s3://$BUCKET_NAME" --index-document index.html --error-document index.html | Out-Null

        # Make bucket public (needed for CloudFront OAI or direct access)
        $publicPolicy = @"
{
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "PublicReadGetObject",
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::$BUCKET_NAME/*"
    }]
}
"@
        # Disable block public access first
        aws s3api put-public-access-block `
            --bucket $BUCKET_NAME `
            --public-access-block-configuration "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false"
        $publicPolicy | aws s3api put-bucket-policy --bucket $BUCKET_NAME --policy file:///dev/stdin 2>$null
        if ($LASTEXITCODE -ne 0) {
            # Write to temp file instead
            $policyFile = "$env:TEMP\s3policy.json"
            $publicPolicy | Out-File -FilePath $policyFile -Encoding utf8
            aws s3api put-bucket-policy --bucket $BUCKET_NAME --policy file://$policyFile
        }
        Write-Host "  S3 bucket created and configured." -ForegroundColor Green
    }

    # Upload dist
    Write-Host "  Uploading frontend build to S3..."
    aws s3 sync dist/ "s3://$BUCKET_NAME" --delete --region $REGION | Out-Null
    Write-Host "  Frontend uploaded to S3." -ForegroundColor Green

    # Check if CloudFront distribution exists
    $cfDists = aws cloudfront list-distributions --output json | ConvertFrom-Json
    $existing = $cfDists.DistributionList.Items | Where-Object {
        $_.Origins.Items | Where-Object { $_.DomainName -like "*$BUCKET_NAME*" }
    }

    if (!$existing) {
        Write-Host "  Creating CloudFront distribution (takes ~10 minutes to deploy globally)..." -ForegroundColor Cyan

        $cfConfig = @{
            Origins = @{
                Quantity = 1
                Items = @(@{
                    Id           = "S3-$BUCKET_NAME"
                    DomainName   = "$BUCKET_NAME.s3-website-$REGION.amazonaws.com"
                    CustomOriginConfig = @{
                        HTTPPort             = 80
                        HTTPSPort            = 443
                        OriginProtocolPolicy = "http-only"
                    }
                })
            }
            DefaultCacheBehavior = @{
                ViewerProtocolPolicy = "redirect-to-https"
                AllowedMethods       = @{ Quantity = 2; Items = @("GET","HEAD") }
                ForwardedValues      = @{ QueryString = $false; Cookies = @{ Forward = "none" } }
                MinTTL               = 0
                DefaultTTL           = 86400
                MaxTTL               = 31536000
                Compress             = $true
                TargetOriginId       = "S3-$BUCKET_NAME"
                TrustedSigners       = @{ Enabled = $false; Quantity = 0 }
            }
            CustomErrorResponses = @{
                Quantity = 1
                Items = @(@{ ErrorCode = 404; ResponseCode = "200"; ResponsePagePath = "/index.html"; ErrorCachingMinTTL = 10 })
            }
            Comment             = "RAKTA-SETU frontend"
            DefaultRootObject   = "index.html"
            Enabled             = $true
            PriceClass          = "PriceClass_100"
            HttpVersion         = "http2"
            CallerReference     = "rakta-setu-$(Get-Date -Format 'yyyyMMddHHmmss')"
        } | ConvertTo-Json -Depth 10 -Compress

        $cfFile = "$env:TEMP\cf-config.json"
        @{ DistributionConfig = ($cfConfig | ConvertFrom-Json) } | ConvertTo-Json -Depth 15 | Out-File $cfFile -Encoding utf8

        $cf = aws cloudfront create-distribution --distribution-config file://$cfFile --output json | ConvertFrom-Json
        $CF_DOMAIN = $cf.Distribution.DomainName
        $CF_ID     = $cf.Distribution.Id

        Write-Host ""
        Write-Host "  ✅ CloudFront distribution created!" -ForegroundColor Green
        Write-Host "  CloudFront domain: https://$CF_DOMAIN" -ForegroundColor Cyan
        Write-Host "  CloudFront ID: $CF_ID" -ForegroundColor Cyan
    } else {
        $CF_DOMAIN = $existing.DomainName
        $CF_ID = $existing.Id
        Write-Host "  CloudFront already exists: https://$CF_DOMAIN" -ForegroundColor Green

        Write-Host "  Invalidating CloudFront cache..."
        aws cloudfront create-invalidation --distribution-id $CF_ID --paths "/*" | Out-Null
        Write-Host "  Cache invalidated." -ForegroundColor Green
    }

    Set-Location $PROJECT_ROOT
}

# ── 7. Summary ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""

if ($AppRunnerUrl) {
    Write-Host "  Backend (App Runner): $AppRunnerUrl" -ForegroundColor Cyan
    Write-Host "  Health check:         $AppRunnerUrl/health" -ForegroundColor Cyan
}
if ($CF_DOMAIN) {
    Write-Host "  Frontend (CloudFront): https://$CF_DOMAIN" -ForegroundColor Cyan
}
Write-Host ""
Write-Host "  NEXT STEPS:" -ForegroundColor Yellow
Write-Host ""
Write-Host "  1. Update Twilio webhook URLs in https://console.twilio.com"
Write-Host "     SMS:   $AppRunnerUrl/twilio/whatsapp/inbound"
Write-Host "     Voice: $AppRunnerUrl/twilio/voice/gather/{neg_id}/{user_id}"
Write-Host ""
if ($CF_DOMAIN) {
    Write-Host "  2. Go to App Runner in AWS Console → Environment Variables"
    Write-Host "     Add: FRONTEND_URL = https://$CF_DOMAIN"
    Write-Host "     (This locks down CORS to your CloudFront domain only)"
    Write-Host ""
}
Write-Host "  3. In the Setup tab of the app, click 'Save' to persist your"
Write-Host "     Twilio config — the real numbers + keys will be stored securely."
Write-Host ""
Write-Host "  4. Set the Twilio secrets in Secrets Manager if you haven't:"
Write-Host "     aws secretsmanager update-secret --secret-id rakta-setu/prod --region us-east-1 --secret-string '{...}'"
Write-Host ""
Write-Host "  Done! 🩸 RAKTA-SETU is live on AWS." -ForegroundColor Green
