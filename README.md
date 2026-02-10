# ArchToolkit

**한국의 고고학자/문화유산 연구자를 위한 QGIS 종합 분석 도구**

> "지식은 전유물이 아닙니다"

ArchToolkit은 한국의 고고학·문화유산 조사/연구 환경에서 자주 쓰이는 작업을 한데 묶은 QGIS 플러그인입니다. DEM 생성부터 지형·가시성·이동(비용)·네트워크·지구화학도 수치화·도면 스타일링까지, 연구자가 분석과 기록에 집중할 수 있도록 도구들을 정리했습니다.

## Citation & Star

이 플러그인이 유용했다면 GitHub Star를 눌러주세요! 개발자에게 큰 힘이 됩니다.  
If you find this repository useful, please consider giving it a star and citing it in your work:

```bibtex
@software{ArchToolkit2026,
  author = {lzpxilfe},
  title = {ArchToolkit: Archaeology Toolkit for QGIS},
  year = {2026},
  url = {https://github.com/lzpxilfe/ar},
  version = {0.1.0}
}
```

## 한눈에 보기

- 버전: **v0.1.0 (beta)**  *(이전 표기 정리: 필요하면 삭제 후 재설치)*
- 권장 환경: **QGIS 3.40 LTR 이상**
- 실행 중 진행상황/경고를 확인할 수 있는 **실시간 작업 로그 창** 제공
- 결과 레이어는 가능하면 `ArchToolkit - ...` 그룹 아래에 정리해서 프로젝트가 덜 어지럽게 설계했습니다.

## 빠른 시작 (Quick Start)

1. 아래 “설치 방법”대로 설치 후 QGIS 재시작
2. DEM이 없으면 `DEM 생성`으로 DEM 준비 (또는 기존 DEM 사용)
3. 필요한 분석 도구(지형/가시권/비용/LCP/네트워크/GeoChem 등) 실행
4. (선택) `AI 조사요약 (AOI Report)`으로 **AOI 반경 내 결과를 보고서 문장**으로 정리

## 도구 목록 (Tools)

분석 흐름 기준으로 묶었습니다.

### 기초 데이터
- **DEM 생성 (Generate DEM)**: 등고선·표고점 기반 DEM 생성(TIN/IDW 등) + (포인트 입력 시) **Kriging(Lite)** 지원(예측 DEM + `_variance.tif` 분산 래스터), 수치지형도(DXF) 코드 프리셋 지원.
- **등고선 추출 (Extract Contours)**: (1) DXF 레이어 필터링 또는 (2) DEM에서 GDAL `gdal_contour` 기반 등고선 생성.

### 지형/단면
- **지형 분석 (Terrain Analysis)**: 경사/사면방향/TRI/TPI/Roughness/Slope Position 분석 + 분류/스타일 적용.
- **AHP 입지적합도 (AHP Suitability)**: 환경변수(래스터)들을 AHP(쌍대비교) 가중치로 통합해 입지 “적합도” 래스터를 생성(기본 0–1, 옵션 0–100). (CR>0.10이면 가중치 일관성 재검토 권장)
- **경사도/사면방향 도면화 (Slope/Aspect Drafting)**: AOI 기준 인쇄용 경사 래스터 + 사면방향(방위각) 화살표 포인트 생성.
- **트렌치 후보 제안 (Trench Suggestion)**: AOI 내 트렌치 폭/길이 조건으로 후보를 자동 제안(등고선 직교 기본, 평행 옵션, AHP/주변 유적 점수, 무덤/분묘 회피 보조).
- **지형 단면 (Terrain Profile)**: 단면선 그리기/저장, 다중 프로파일, 지도-차트 연동(hover), AOI/벡터 오버레이 + 경사/누적상승/구간 통계 + CSV/이미지(PNG/JPG) export.

### 가시성
- **가시권 분석 (Viewshed Analysis)**: 단일/누적/역방향/선형 가시권 + 가시선(LOS) 단면(프로파일), 히구치 거리대, 곡률·굴절 옵션 + (옵션) AOI 가시 통계(가시면적/가시비율) + 가중 누적/표준화(0–100%).

### 이동/네트워크
- **비용표면/최소비용경로 (Cost Surface / LCP)**: DEM 경사 기반 이동 시간/에너지 모델링 + LCP + Least-cost corridor(회랑) + 추가 마찰(래스터/벡터) + 등시간선/등에너지선(옵션).
- **최소비용 네트워크 (Least-cost Network)**: 유적 간 LCP 기반 MST/k-NN/Hub 네트워크 생성 + (옵션) 중심성 지표(SNA).
- **근접/가시성 네트워크 (PPA / Visibility)**: 근접성(PPA) 그래프 + DEM 기반 상호가시성(Visibility) 그래프 생성.

### 참고/보조 도구
- **지적도 중첩 면적표 (Cadastral Overlap)**: 조사지역×필지 중첩 면적/비율 계산 + 중첩(클립) 레이어 생성.
- **도면 시각화 (Map Styling)**: 한국 수치지형도(DXF) 레이어 집계/분류 + 도로·하천·건물 카토그래피 스타일 + DEM 배경 스타일(옵션) + QML/프리셋 내보내기 + DXF 코드 매핑(JSON) 커스터마이즈.
- **지구화학도 래스터 수치화 (GeoChem WMS → Raster)**: WMS RGB(범례 기반) 수치화 → value/class 래스터 + (옵션) 구간별 폴리곤/중심점 생성.
- **지질도 도엽 ZIP 불러오기/래스터 변환 (KIGAM)**: KIGAM 1:50,000 지질도 ZIP에서 SHP 자동 로드 + sym 스타일/라벨 적용 + 벡터→래스터(MaxEnt/예측모델) 변환.
- **AI 조사요약 (AOI Report)**: 조사지역(AOI) + 반경(m) 내 레이어 요약 → 보고서/업무 메모 문장 생성(무료: 로컬 요약 / 옵션: Gemini API / 통계 CSV·번들 저장 / 추가 유적 관계 분석 / 대형 유적 내부·외부 분리 요약).

## Kriging(Lite) (DEM 생성에서)

`DEM 생성` 도구의 보간 방식에서 `Kriging (Lite, Ordinary)`를 선택하면, **포인트 값(해발 등)**을 기반으로 DEM을 만들 수 있습니다.

- 입력: 포인트 레이어 + 값 필드(Z) (또는 3D 포인트의 Z)
- 권장: **투영 CRS(미터 단위)** + 너무 촘촘하지 않은 픽셀 크기(대상 범위가 넓으면 픽셀을 키우는 것이 안전)
- 출력: 예측 DEM `.tif` + 같은 경로에 `_variance.tif`(분산/불확실성)도 함께 생성
- Lite 특성: 자동 파라미터 + 셀마다 근처 N개 점만 사용(기본 16, 크게 하면 느려질 수 있음)

## Map Styling 커스터마이즈

- DXF 코드/선폭/라벨 매핑은 `tools/map_styling_codes.json`에서 수정할 수 있습니다. (다이얼로그의 “다시 불러오기”로 즉시 반영)
- `📦 QML/프리셋 내보내기...` 버튼으로 스타일 QML(도로/하천/건물)과 현재 코드 매핑 JSON을 폴더로 저장할 수 있습니다. (DEM 스타일은 DEM 선택+체크 시 함께 저장)

## 지질도 도엽 ZIP 불러오기/래스터 변환 (KIGAM)

KIGAM 1:50,000 지질도 도엽 ZIP(보통 SHP 묶음)을 프로젝트에 바로 불러오고, 예측모델링/MaxEnt 입력으로 쓸 수 있게 **범주형 래스터**로 변환합니다.

### 1) ZIP 불러오기

1. `지질도 도엽 ZIP 불러오기/래스터 변환 (KIGAM)` 실행
2. ZIP 선택 → `ZIP 불러오기`
3. 레이어가 `ArchToolkit - Geology` → `KIGAM_<도엽명>` 아래에 정리됩니다.
   - 라인/포인트가 폴리곤(Litho) 위로 오도록 순서를 맞춰서, **Litho에 가려지지 않도록** 설계했습니다.
   - `Frame`, `Crosssection` 같은 보조 레이어는 기본 숨김(필요할 때만 켜세요)

### 2) 벡터 → 래스터

- 기본 레이어 목록은 **KIGAM Litho(폴리곤)** 위주로 보여주고, 레이어 앞에 `[GF13_청주]`처럼 **도엽/지역**을 함께 표시합니다.
- 값 필드 추천: `LITHOIDX` 또는 `AGEIDX`
  - 문자 코드(예: `Qa`, `Jbgr`)는 자동으로 1,2,3… 정수로 매핑해서 래스터에 기록합니다.
  - 같은 경로에 `*_mapping.csv`를 함께 저장합니다. (열: `code,int_value,label,feature_count`)
- 여러 레이어를 선택했다면 `선택 레이어 병합 후 단일 래스터`(병합) 또는 `레이어별 래스터 출력` 중에서 선택할 수 있습니다.
- 해상도(픽셀 크기)는 **미터 단위 투영 CRS**에서 설정하는 것을 권장합니다.

### 3) 모델링 팁

- 지질 단위는 보통 연속형이 아니라 **범주형 변수**입니다.
  - MaxEnt를 쓴다면 해당 레이어를 categorical로 지정하세요.
  - 다른 ML에서 one-hot이 필요하면 `*_mapping.csv`를 기준으로 더미 변수를 생성하세요.
- 다중 변수(예: 지질 + 지구화학 + 지형)로 모델을 만들 때는 모든 래스터의 **좌표계/해상도/Extent**를 맞추는 것이 중요합니다.

### 문제 해결

- CSV는 나오는데 래스터가 안 보이면: 로그 파일 `%APPDATA%\QGIS\QGIS3\profiles\default\ArchToolkit\logs\archtoolkit.log`에서 `KIGAM rasterize result` 로그를 확인하세요.
- 지질도 레이어 CRS가 `EPSG:4326`(경위도)인 경우, 픽셀 크기(m)는 내부적으로 도(°) 단위로 **자동 환산**해 처리합니다. 정확한 미터 해상도가 중요하면 **투영 CRS(미터 단위)** 로 변환해서 작업하는 것을 권장합니다.
- 출력 폴더 권한/보안 설정에 따라 특정 폴더(예: Desktop/Documents)에서 쓰기 실패가 날 수 있습니다. 이 경우 다른 폴더(예: Downloads)에 저장해보세요.

## AHP 입지적합도 (AHP Suitability)

여러 환경변수(래스터)를 **AHP(쌍대비교) 가중치**로 통합해 “입지 적합도” 래스터를 생성합니다.

- 정규화: 각 기준을 `min~max`로 0–1 스케일로 정규화 (Benefit: 값↑ 좋음 / Cost: 값↓ 좋음)
- 가중치: 쌍대비교 표로 가중치 계산(일관성비율 CR 제공)
- 출력: 가중합 적합도(기본 0–1, 옵션 0–100) 래스터 + `ArchToolkit - AHP` 그룹에 정리 + 메타데이터 태깅

사용 흐름:
1. (선택) AOI 지정 + `AOI 범위로 자르기` 체크(권장)
2. 기준 래스터 추가(각 기준마다 Benefit/Cost 방향 지정)
3. 필요 시 `통계 계산(min/max)` 실행(미계산이면 실행 시 자동 계산)
4. 쌍대비교 표 입력 → CR이 **0.10 초과**면 가중치 일관성 재검토 권장
5. `실행` → 결과 래스터 생성

## AI 조사요약 (AOI Report)

### 왜 넣었나요?
분석을 여러 번 돌리고 나면 결과가 레이어로 잔뜩 쌓입니다. `AI 조사요약`은 AOI(조사지역) 주변의 결과 레이어를 **한 번에 요약**해서, 현장 기록/보고서 초안으로 바로 쓸 수 있는 형태로 정리하려고 만들었습니다.

### 무엇을 “읽나요”? (현재 구조)
현재는 “프로젝트 레이어를 스캔해서 AOI 반경 내 통계를 모으는 방식”입니다.

- 벡터 레이어: AOI 버퍼와 교차하는 **피처 수**, (가능하면) **길이/면적 합**, 일부 필드(`class_id`, `Layer`, `element`)의 상위 값 분포
- 래스터 레이어: AOI 버퍼 내부의 **min/mean/max** 등 단순 통계(가능하면)
- 추가 유적 레이어(옵션): AOI와의 관계(`AOI 내부`, `AOI 경계 걸침`, `버퍼 내부`, `버퍼 경계 걸침`, `버퍼 외부`)와 거리 요약
- 대형 폴리곤/선형 유적(옵션): AOI **내부/외부 면적·길이** 및 비율(%) 분리 요약

즉, **결과가 래스터/벡터 레이어로 존재**하면 대부분 요약에 포함될 수 있습니다. 다만 “각 도구의 의미를 100% 이해해서 해석”하는 수준까지는 아니고, 기본적으로는 **레이어 기반 통계를 모아 문장화**하는 구조입니다.

### 정확도 향상: ArchToolkit 메타데이터
ArchToolkit 주요 도구는 결과 레이어에 표준 메타데이터를 붙입니다(레이어 customProperty).

- `archtoolkit/tool_id`: 어떤 도구가 만든 결과인지
- `archtoolkit/run_id`: 같은 실행(run)에서 나온 결과를 묶기 위한 ID
- `archtoolkit/kind`: 결과 타입(예: viewshed, cost_surface, corridor 등)
- `archtoolkit/units`: 값의 단위(가능한 경우)
- `archtoolkit/params_json`: 입력 파라미터(가능한 경우)

AI 조사요약(로컬/Gemini)은 가능하면 이 메타데이터를 우선 사용해 의미를 해석하고, 동일 `run_id` 결과를 묶어서 설명합니다.

### 모드(티어)

- `무료(로컬 요약)`: 외부 전송 없이 로컬에서 통계를 문장으로 정리 (문장 품질/해석은 제한적)
- `Gemini(API)`: 더 자연어 중심의 보고서 문장 생성 (API 키 필요, 아래 주의 참고)

### 추가 유적 관계 분석(신규)

- AI 입력에서 `추가 유적(관계 분석) 포함`을 켜고, 참조할 유적 레이어를 선택할 수 있습니다.
- `추가 유적: 선택 피처만 사용`을 켜면, 지도에서 선택한 유적만 관계 분석에 포함됩니다.
- 이름 필드와 최대 피처 수를 지정해 보고서에 들어갈 대상을 제어할 수 있습니다.
- 성곽처럼 면적이 큰 유적이나 선형 유적은 AOI 내부/외부를 분리해 해석할 수 있도록 내부·외부 비율을 함께 제공합니다.
- 긴 작업 중에는 바쁜 상태 라벨과 진행바를 표시해, 멈춘 것처럼 보여도 처리 중인지 확인할 수 있습니다.

### Gemini 모드 주의(프라이버시/키)

- Gemini API를 사용할 경우, **AOI 반경 내 요약 정보(레이어 이름/카운트/통계)**가 외부 API로 전송됩니다.
  - 원본 래스터/벡터 전체를 업로드하지 않도록 설계했지만, 프로젝트에 따라 민감정보가 레이어명/속성에 포함될 수 있으니 주의하세요.
- API 키는 QGIS **인증 저장소(QgsAuthManager)**에 저장하도록 구현했습니다.
  - 한 번 저장하면 다음 실행부터는 자동으로 불러옵니다(필요할 때만 변경).

1. `ArchToolkit` 메뉴에서 **AI 조사요약 (AOI Report)** 실행
2. `조사지역 폴리곤(AOI)` 레이어 선택 (가능하면 **투영 CRS(미터 단위)** 사용)
3. (선택) 대상 레이어가 너무 많거나 섞여 있으면, `대상 레이어`를 **자동 / 그룹 지정 / 레이어 직접 선택**으로 조정
4. (선택) `추가 유적(관계 분석) 포함`을 켜고 유적 레이어/이름 필드/최대 개수를 지정
5. `모드` 선택: `무료(로컬 요약)` 또는 `Gemini(API)`
6. (Gemini 모드인 경우) **API 키 설정/변경…**에서 키 입력
7. 반경(m) 설정 → **AI 요약 생성**
8. 필요 시 `저장…`/`통계 CSV…`/`번들 저장…`으로 Markdown/CSV/스냅샷 번들을 내보내기

### 통계 CSV / 번들 저장
AI 없이도 “AOI 반경 내 표준 통계표”를 바로 뽑아, 보고서 뼈대를 만들 수 있습니다.

- `통계 CSV…`: 레이어 1행 요약(`layers_summary`) + 수치 필드 통계(`numeric_fields`) CSV 저장
- `번들 저장…`: `report.md` + `context.json` + `params.json` + CSV + `canvas.png`를 한 폴더로 내보내기 (현장기록/보고서용)

## Gemini API 키 발급(받는 법)

ArchToolkit의 `Gemini(API)` 모드는 **Google Gemini API 키**가 있어야 동작합니다. (`무료(로컬 요약)`은 키가 필요 없습니다.)

1. Google AI Studio에 로그인합니다.
   - https://aistudio.google.com/
2. **API Keys** 페이지로 이동해 `Create API key`를 눌러 키를 생성합니다.
   - https://aistudio.google.com/app/apikey
3. 생성된 키를 복사한 뒤, QGIS에서 `AI 조사요약 (AOI Report)` 창의 **API 키 설정/변경…** 버튼으로 입력합니다.

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

현재는 QGIS 공식 플러그인 저장소 배포를 준비 중이며, 당분간은 **GitHub 기반 수동 설치**를 권장합니다.

### 수동 설치(개발용)

- 이 저장소를 QGIS 플러그인 디렉터리에 `ArchToolkit` 폴더명으로 복사(또는 `git clone`)한 뒤 QGIS를 재시작합니다.
  - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\ArchToolkit`
  - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/ArchToolkit`
  - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/ArchToolkit`

업데이트(수동 설치):
- `git clone`으로 설치했다면 해당 폴더에서 `git pull` 후 QGIS 재시작
- ZIP으로 복사했다면 기존 `ArchToolkit` 폴더를 교체 후 QGIS 재시작

## 사용/해석 주의

- 가시권/LOS/비용/네트워크 결과는 DEM 해상도·좌표계·고도 품질에 크게 의존합니다.
- 비용/네트워크 도구는 기본적으로 “경사 기반 이동 비용”을 사용하며, (비용표면 도구의 옵션) 마찰 레이어로 도로/식생/토지피복 등의 가중을 일부 반영할 수 있습니다(근사).
- GeoChem 도구는 WMS의 색상(RGB)을 범례 기준으로 수치화한 **추정치**입니다(원자료 측정값이 아닙니다).
- AHP 입지적합도는 **선택한 환경변수/정규화 범위/가중치(쌍대비교)에 따라 달라지는 상대지표**입니다. 보고서에는 사용한 기준 레이어와 CR(일관성비율), 정규화 방식(AOI 범위 여부)을 함께 기록하는 것을 권장합니다.
- 지적도 도구: 연속지적도/공간정보 데이터는 **참고용**입니다. 법적 효력/정확 경계 확인은 관할 시·군·구청(시청/구청)에서 발급받은 **공식 지적도/대장**을 확인하세요.
- AI 조사요약: AI/요약 결과는 참고용이며, 최종 해석/기술은 사용자가 책임지고 검토해야 합니다.

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

### Viridian City(복귀 지점) 운영

이 저장소는 “언제든 돌아갈 수 있는 집”을 `viridian-city` 태그로 관리합니다.

- `viridian-city`: 최신 안정 상태를 가리키는 *움직이는 태그* (필요하면 `-f`로 갱신)
- `viridian-city-YYYYMMDD-v0.1.0-N`: 그날의 스냅샷(되돌리기/비교용, 고정)

> GitHub에서 보이는 README는 이 `README.md`입니다. 수정 후 `main`에 커밋/푸시하면 GitHub에 바로 반영됩니다.

```bash
# (1) 현재 상태를 Viridian City(집)로 지정
git tag -a -f viridian-city -m "Viridian City home"
git push -f origin viridian-city

# (2) 스냅샷 태그 추가(예시)
git tag -a viridian-city-20260208-v0.1.0-1 -m "Viridian City snapshot"
git push origin viridian-city-20260208-v0.1.0-1

# (3) 복귀
git switch main
git reset --hard viridian-city
```

> 태그 자체는 “포인터”라 용량을 거의 늘리지 않습니다. 다만 큰 바이너리 파일(이미지/래스터 등)을 자주 커밋하면 저장소 용량은 커질 수 있습니다.

## 라이선스

이 프로젝트는 **GNU GPL v3** 라이선스를 따릅니다. 
"지식은 전유물이 아니다"라는 제작자의 철학에 따라, 누구나 자유롭게 사용하고, 수정하며, 공유할 수 있습니다.

## 기여하기

피드백과 기여는 언제나 환영합니다. 이슈(Issues)를 통해 버그 제보나 기능 제안을 남겨주세요.

---
© 2026 balguljang2.
