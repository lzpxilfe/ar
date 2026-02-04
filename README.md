# ArchToolkit

**한국의 고고학자와 문화유산 연구자를 위한 QGIS 종합 분석 도구**

> "지식은 전유물이 아닙니다"

ArchToolkit은 한국의 고고학 연구 환경에 최적화된 다양한 분석 및 시각화 기능을 제공하는 QGIS 플러그인입니다. 복잡한 수치지형도 처리부터 고도의 지형 분석까지, 연구자가 오로지 연구에만 집중할 수 있도록 돕는 도구들을 모았습니다.

## 도구 목록 (Tools)

- **DEM 생성 (Generate DEM)**: 등고선·표고점 기반 DEM 생성(TIN/IDW 등), 수치지형도(DXF) 코드 프리셋 지원.
- **등고선 추출 (Extract Contours)**: (1) DXF 레이어 필터링 또는 (2) DEM에서 GDAL `gdal_contour` 기반 등고선 생성.
- **지적도 중첩 면적표 (Cadastral Overlap)**: 조사지역×필지 중첩 면적/비율 계산 + 중첩(클립) 레이어 생성.
- **지형 분석 (Terrain Analysis)**: 경사/사면방향/TRI/TPI/Roughness/Slope Position 분석 + 분류/스타일 적용.
- **경사도/사면방향 도면화 (Slope/Aspect Drafting)**: AOI 기준 인쇄용 경사 래스터 + 사면방향(방위각) 화살표 포인트 생성.
- **지형 단면 (Terrain Profile)**: 단면선 그리기/저장, 다중 프로파일, 지도-차트 연동(hover), AOI/벡터 오버레이 + 경사/누적상승/구간 통계 + CSV/이미지(PNG/JPG) export.
- **가시권/가시선 (Viewshed / LOS)**: 단일/누적/역방향/선형 가시권, 가시선(LOS) + 프로파일, 히구치 거리대, 곡률·굴절 옵션 + (옵션) AOI 가시 통계(가시면적/가시비율) + 가중 누적/표준화(0–100%).
- **비용표면/최소비용경로 (Cost Surface / LCP)**: DEM 경사 기반 이동 시간/에너지 모델링 + LCP + Least-cost corridor(회랑) + 추가 마찰(래스터/벡터) + 등시간선/등에너지선(옵션).
- **최소비용 네트워크 (Least-cost Network)**: 유적 간 LCP 기반 MST/k-NN/Hub 네트워크 생성 + (옵션) 중심성 지표(SNA).
- **근접/가시성 네트워크 (PPA / Visibility)**: 근접성(PPA) 그래프 + DEM 기반 상호가시성(Visibility) 그래프 생성.
- **도면 시각화 (Map Styling)**: 한국 수치지형도(DXF) 레이어 집계/분류 + 도로·하천·건물 카토그래피 스타일 + DEM 배경 스타일(옵션) + QML/프리셋 내보내기 + DXF 코드 매핑(JSON) 커스터마이즈.
- **지구화학도 래스터 수치화 (GeoChem WMS → Raster)**: WMS RGB(범례 기반) 수치화 → value/class 래스터 + (옵션) 구간별 폴리곤/중심점 생성.
- **AI 조사요약 (Gemini AOI Report)**: 조사지역(AOI) + 반경(m) 내의 ArchToolkit 결과(및 선택 레이어) 요약 → 보고서 형태 문장 생성(옵션: Gemini API).

대부분의 도구는 실행 중 **실시간 작업 로그 창**을 띄워 진행 상황과 경고/오류를 확인할 수 있습니다.

## Map Styling 커스터마이즈

- DXF 코드/선폭/라벨 매핑은 `tools/map_styling_codes.json`에서 수정할 수 있습니다. (다이얼로그의 “다시 불러오기”로 즉시 반영)
- `📦 QML/프리셋 내보내기...` 버튼으로 스타일 QML(도로/하천/건물)과 현재 코드 매핑 JSON을 폴더로 저장할 수 있습니다. (DEM 스타일은 DEM 선택+체크 시 함께 저장)

## AI 조사요약(Gemini) 주의

- Gemini API를 사용할 경우, **AOI 반경 내 요약 정보(레이어 이름/카운트/통계)**가 외부 API로 전송됩니다. (원본 래스터/벡터 전체를 업로드하지 않도록 설계했지만, 프로젝트에 따라 민감정보가 레이어명/속성에 포함될 수 있으니 주의하세요.)
- API 키는 QGIS **인증 저장소(QgsAuthManager)**에 저장하도록 구현했습니다.

## AI 조사요약(Gemini) 사용 방법

1. `ArchToolkit` 메뉴에서 **AI 조사요약 (Gemini AOI Report)** 실행
2. `조사지역 폴리곤(AOI)` 레이어 선택 (가능하면 **투영 CRS(미터 단위)** 사용)
3. 반경(m) 설정 → **AI 요약 생성**
4. 필요 시 `저장…`으로 Markdown/Text 파일로 내보내기

## Gemini API 키 발급(받는 법)

ArchToolkit의 “AI 조사요약(Gemini)” 기능은 **Google Gemini API 키**가 있어야 동작합니다.

1. Google AI Studio에 로그인합니다.
   - `https://aistudio.google.com/`
2. **API Keys** 페이지로 이동해 `Create API key`를 눌러 키를 생성합니다.
   - `https://aistudio.google.com/app/apikey`
3. 생성된 키를 복사한 뒤, QGIS에서 `AI 조사요약 (Gemini AOI Report)` 창의 **API 키 설정/변경…** 버튼으로 입력합니다.

### 주의(보안/과금)

- API 키는 비밀번호처럼 취급하세요. **깃(Git)이나 문서에 키를 그대로 남기지 마세요.**
- 사용량/요금/제한(쿼터)은 Google AI Studio의 **Usage / Limits**에서 확인할 수 있습니다. (프로젝트/계정 상태에 따라 과금이 발생할 수 있습니다.)

### 키 삭제/변경

- 키 변경: `AI 조사요약` 창에서 **API 키 설정/변경…**
- 키 완전 삭제(권장): QGIS `설정 → 옵션 → 인증(Authentication)`에서 `ArchToolkit Gemini` 항목을 찾아 삭제

## 설치 방법

### 요구 사항

- QGIS 3.40 LTR 이상 (현재 개발/테스트 기준)
- QGIS Processing 프레임워크 + GDAL 프로바이더 (기본 포함)
- Python 패키지 `numpy` (대부분의 QGIS 배포판에 기본 포함 — 별도 설치 불필요)
- 외부 플러그인/라이브러리(예: GRASS/SAGA/WhiteboxTools, pandas/matplotlib 등) 없이 QGIS 기본 구성만으로 동작하는 것을 목표로 합니다. (자세한 내용: `DEVELOPMENT.md`)

1.  QGIS를 실행합니다.
2.  `플러그인` > `플러그인 관리 및 설치`를 선택합니다.
3.  `설정` 탭에서 '실험적 플러그인 표시'를 체크합니다.
4.  (현재 준비 중) GitHub 리포지토리를 통해 수동 설치하거나 QGIS 공식 리포지토리에서 검색할 수 있습니다.

### 수동 설치(개발용)

- 이 저장소를 QGIS 플러그인 디렉터리에 `ArchToolkit` 폴더명으로 복사한 뒤 QGIS를 재시작합니다.
  - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\ArchToolkit`
  - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/ArchToolkit`
  - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/ArchToolkit`

## 사용/해석 주의

- 가시권/LOS/비용/네트워크 결과는 DEM 해상도·좌표계·고도 품질에 크게 의존합니다.
- 비용/네트워크 도구는 기본적으로 “경사 기반 이동 비용”을 사용하며, (비용표면 도구의 옵션) 마찰 레이어로 도로/식생/토지피복 등의 가중을 일부 반영할 수 있습니다(근사).
- GeoChem 도구는 WMS의 색상(RGB)을 범례 기준으로 수치화한 **추정치**입니다(원자료 측정값이 아닙니다).

## 참고 문서

- 학술 출처: `REFERENCES.md`
- 개발 원칙(외부 의존성 최소화): `DEVELOPMENT.md`
- 안정성/스모크 테스트: `STABILITY.md`, `SMOKE_TEST.md`

## 개발자용: Git/GitHub 연동 팁

- 이 저장소는 안정 브랜치(`main`)와 작업 브랜치(`work/*`)를 분리해서 운영하는 것을 권장합니다. (자세한 내용: `STABILITY.md`)
- GitHub에서 “변경이 안 보인다”면 **브랜치가 `main`인지 `work/*`인지** 먼저 확인하세요.
- 내 GitHub 계정의 `ar` 저장소로 푸시하려면, 원격(remote)이 내 저장소를 가리키도록 설정해야 합니다. 예:
  - 포크/업스트림 방식(권장): `origin`=내 저장소, `upstream`=원본 저장소
  - 단순 추가 방식: `my` 같은 이름으로 내 저장소 remote를 추가하고 해당 remote로 `git push`

```bash
# 현재 연결/브랜치 확인
git remote -v
git branch -vv

# (예시) 내 저장소로 푸시하기: my remote 추가
git remote add my https://github.com/<YOUR_GITHUB_ID>/ar.git
git push -u my work/geochem
git push my --tags
```

## 라이선스

이 프로젝트는 **GNU GPL v3** 라이선스를 따릅니다. 
"지식은 전유물이 아니다"라는 제작자의 철학에 따라, 누구나 자유롭게 사용하고, 수정하며, 공유할 수 있습니다.

## 기여하기

피드백과 기여는 언제나 환영합니다. 이슈(Issues)를 통해 버그 제보나 기능 제안을 남겨주세요.

---
© 2026 balguljang2.
