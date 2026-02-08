# ArchToolkit 학술 참고문헌

본 플러그인에서 사용하는 보간 알고리즘의 원 출처입니다.

## TIN 선형 보간 (Triangulated Irregular Network)

**들로네 삼각분할:**
> Delaunay, B. (1934). "Sur la sphère vide". *Otdelenie Matematicheskikh i Estestvennykh Nauk*, 7, pp. 793–800.

**GIS 적용:**
> Fowler, R.J., & Little, J.J. (1979). "Automatic extraction of irregular network digital terrain models". *Computer Graphics (SIGGRAPH '79)*, 13(2), pp. 199–207.

## TIN 곡면 보간 (Clough-Tocher)

> Clough, R.W., & Tocher, J.L. (1965). "Finite element stiffness matrices for analysis of plates in bending". *Proceedings of the Conference on Matrix Methods in Structural Mechanics*, Wright-Patterson AFB, Ohio.

## IDW (역거리 가중치)

> Shepard, D. (1968). "A two-dimensional interpolation function for irregularly-spaced data". *Proceedings of the 1968 23rd ACM National Conference*, pp. 517–524. DOI: 10.1145/800186.810616

## 크리깅 보간 (Kriging / Geostatistical Interpolation)

**지오통계/변동함수(variogram) 기반 보간:**
> Matheron, G. (1963). "Principles of geostatistics." *Economic Geology*, 58(8), pp. 1246–1266.

> Cressie, N. (1993). *Statistics for Spatial Data*. Wiley.

## 등고선 생성 (Contour Generation)

**GDAL Contour:**
> GDAL Development Team (2024). "GDAL - Geospatial Data Abstraction Library". Open Source Geospatial Foundation. https://gdal.org

**등고선 추출 알고리즘:**
> 등고선 생성은 래스터 DEM에서 동일 표고점을 연결하는 표준 GIS 기법으로, GDAL의 `gdal_contour` 유틸리티를 활용합니다.

## 경사도 분석 (Slope Analysis)

**Tobler의 하이킹 함수 (보행 속도):**
> Tobler, W. (1993). "Three Presentations on Geographical Analysis and Modeling: Non-Isotropic Geographic Modeling, Speculations on the Geometry of Geography, Global Spatial Analysis." *NCGIA Technical Report 93-1*.

**Naismith의 규칙 (시간 기반 보행 모델):**
> Naismith, W. W. (1892). "Excursions." *Scottish Mountaineering Club Journal*.

**Conolly & Lake의 상대 경사 비용 (Relative slope cost):**
> Conolly, J., & Lake, M. (2006). *Geographical Information Systems in Archaeology*. Cambridge University Press.

**Herzog 이동 비용 함수(메타볼릭/차량) 구현 참고:**
> Čučković, Z. (2024). *Movement Analysis* (QGIS plugin). https://github.com/zoran-cuckovic/QGIS-movement-analysis/

**Minetti의 에너지 효율 연구:**
> Minetti, A.E. (2002). "The three modes of terrestrial locomotion." In: *Running & Science*. Cambridge University Press.

**Llobera & Sluckin의 인지적 경사 연구:**
> Llobera, M. & Sluckin, T.J. (2007). "Zigzagging: Theoretical insights on climbing strategies." *Journal of Theoretical Biology*, 249(2), pp. 206-217.

## AHP (Analytic Hierarchy Process)

**쌍대비교 기반 다기준 의사결정(가중치/일관성비율):**
> Saaty, T.L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.

## 최소비용 네트워크 (Least-cost Network)

**MST(최소 신장 트리) 알고리즘:**
> Kruskal, J.B. (1956). "On the shortest spanning subtree of a graph and the traveling salesman problem." *Proceedings of the American Mathematical Society*, 7(1), pp. 48–50.

> Prim, R.C. (1957). "Shortest connection network and some generalizations." *Bell System Technical Journal*, 36(6), pp. 1389–1401.

## 사회 네트워크 분석 (SNA: Social Network Analysis)

**중심성(centrality) 개념:**
> Freeman, L.C. (1979). "Centrality in social networks: Conceptual clarification." *Social Networks*, 1(3), pp. 215–239.

**SNA 방법론(개론/표준 참고서):**
> Wasserman, S., & Faust, K. (1994). *Social Network Analysis: Methods and Applications*. Cambridge University Press.

> Newman, M.E.J. (2010). *Networks: An Introduction*. Oxford University Press.

**Betweenness 계산(Brandes 알고리즘):**
> Brandes, U. (2001). "A faster algorithm for betweenness centrality." *Journal of Mathematical Sociology*, 25(2), pp. 163–177.

## 근접성 네트워크 (PPA: Proximal Point Analysis)

> Terrell, J.E. (1977). "Human Biogeography in the Solomon Islands." *Fieldiana Anthropology*, 68(1), pp. 1–47.

> Brughmans, T., & Peeples, M.A. (2017). "Trends in archaeological network research: a bibliometric analysis." *Journal of Historical Network Research*, 1, pp. 1–24. DOI: 10.25517/jhnr.v1i1.10

> Amati, V., Shafie, T., & Brandes, U. (2018). "Reconstructing Archaeological Networks with Structural Holes." *Journal of Archaeological Method and Theory*, 25, pp. 226–253. DOI: 10.1007/s10816-017-9335-1

## 가시성 네트워크 (Visibility / Intervisibility Network, VGA)

> Van Dyke, R.M., Bocinsky, R.K., Windes, T.C., & Robinson, T.J. (2016). "Great houses, shrines, and high places: intervisibility in the Chacoan world." *American Antiquity*, 81(2), pp. 205–230.

> Gillings, M., & Wheatley, D. (2001). "Seeing is not believing: unresolved issues in archaeological visibility analysis." In: *On the Good Use of Geographical Information Systems in Archaeological Landscape Studies* (COST Action G2).

> Turner, A., Doxa, M., O'Sullivan, D., & Penn, A. (2001). "From isovists to visibility graphs: a methodology for the analysis of architectural space." *Environment and Planning B: Planning and Design*, 28(1), pp. 103–121. DOI: 10.1068/b2684

## 가시권 분석 (Viewshed / LOS)

> Wang, J., Robinson, G. J., & White, K. (1996). "A Fast Solution to Local Viewshed Computation Using Grid-Based Digital Elevation Models." *Photogrammetric Engineering & Remote Sensing*, 62(10), pp. 1157–1164.

## 히구치 거리대 (Higuchi view zones)

> Higuchi, T. (1975). *The Visual and Spatial Structure of Landscapes*.

## 지형 거칠기 지수 TRI (Terrain Ruggedness Index)

**Riley et al. 분류 (5등급):**
> Riley, S.J., DeGloria, S.D., & Elliot, R. (1999). "A terrain ruggedness index that quantifies topographic heterogeneity." *Intermountain Journal of Sciences*, 5(1-4), pp. 23-27.

## 지형 위치 지수 TPI (Topographic Position Index)

**Weiss 분류 (표준편차 기반):**
> Weiss, A. (2001). "Topographic Position and Landforms Analysis." *Poster presentation, ESRI User Conference*, San Diego, CA.

## 지형 거칠기 Roughness

**Wilson et al. Geomorphometry:**
> Wilson, J.P., & Gallant, J.C. (2000). "Terrain Analysis: Principles and Applications." *John Wiley & Sons*.

---
*ArchToolkit은 QGIS Processing Framework와 GDAL을 활용합니다.*
