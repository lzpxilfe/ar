# ArchToolkit 개발 철학

## 핵심 원칙: 외부 의존성 없는 순수 QGIS 플러그인

ArchToolkit은 **기본 QGIS 설치만으로 완전히 동작**해야 합니다.

### 사용 가능한 도구
- ✅ **GDAL 알고리즘** (`gdal:slope`, `gdal:aspect`, `gdal:rastercalculator` 등)
- ✅ **QGIS Native 알고리즘** (`native:mergevectorlayers`, `native:buffer` 등)
- ✅ **PyQt/Qt 기본 라이브러리**
- ✅ **Python 표준 라이브러리** (os, tempfile, json 등)
- ✅ **QGIS Core/GUI 라이브러리**

### 사용 금지
- ❌ GRASS GIS 알고리즘 (`grass7:*`)
- ❌ SAGA GIS 알고리즘 (`saga:*`)
- ❌ WhiteboxTools
- ❌ 외부 Python 패키지 (numpy, pandas, matplotlib 등)
- ❌ 별도 설치가 필요한 모든 의존성

### 복잡한 분석 구현 방법
외부 도구가 필요한 기능은 다음 방법으로 대체:

1. **래스터 계산기 활용** (`gdal:rastercalculator`)
   - 수식 조합으로 복잡한 래스터 연산 구현
   
2. **리샘플링 트릭** (`gdal:warpreproject`)
   - 다운샘플링 + 업샘플링으로 focal 연산 근사
   
3. **벡터-래스터 변환 활용**
   - 래스터화, 벡터화를 조합한 분석

4. **반복 처리**
   - Python 루프로 단순 연산을 반복 적용

### 목표
> "QGIS만 설치하면 누구나 ArchToolkit을 바로 사용할 수 있어야 한다"

---

## 학술적 출처 표시 원칙

ArchToolkit은 **도구 모음집**입니다. 우리는 톱과 망치, 가위를 정리해두는 도구상자를 만드는 것이지, 톱과 망치를 발명한 사람이 아닙니다.

### 반드시 지켜야 할 것
- ✅ 알고리즘 원저자 인용 (예: `Weiss 2001`, `Riley 1999`, `Tobler 1993`)
- ✅ 분류 체계의 출처 명시 (한국표준, 학술 논문 등)
- ✅ UI에 저자명 표시 (체크박스, 레이어 이름 등)
- ✅ 코드 주석에 참고문헌 기재

### 왜 중요한가?
1. **학술적 정당성**: 연구자들의 노력을 존중
2. **신뢰성**: 사용자가 방법론의 근거를 알 수 있음
3. **지식 공유**: "지식은 전유물이 아니다"
4. **재현가능성**: 동일한 방법론을 다른 도구로도 구현 가능

### 예시
```python
# TPI (Topographic Position Index)
# Weiss, A. D. (2001). Topographic Position and Landforms Analysis.
# ESRI International User Conference, San Diego, CA.
```

```
☑ 경사도 분류 - Tobler(1993) 보행속도 기반
```

---
*"지식은 전유물이 아닙니다"*

