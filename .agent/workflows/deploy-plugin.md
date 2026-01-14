---
description: QGIS 플러그인(ArchToolkit)을 업데이트하고 설치하는 방법
---

# ArchToolkit 플러그인 배포

플러그인을 수정한 후 QGIS에 반영하려면 아래 단계를 따르세요.

## 1. 기존 플러그인 삭제 후 새로 복사
// turbo
```powershell
Remove-Item -Path "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ArchToolkit" -Recurse -Force -ErrorAction SilentlyContinue; Copy-Item -Path "c:\Users\nuri9\.gemini\antigravity\scratch\ArchToolkit" -Destination "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ArchToolkit" -Recurse -Force
```

## 2. 버전 확인
// turbo
```powershell
Get-Content "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\ArchToolkit\metadata.txt" | Select-String "version"
```

## 3. QGIS 재시작
QGIS를 완전히 종료하고 다시 시작하세요.

## 중요 사항
- 수정할 때마다 metadata.txt의 version을 0.0.1씩 올려야 합니다
- QGIS가 실행 중이면 플러그인 폴더가 잠겨있을 수 있으므로 반드시 종료 후 배포하세요
