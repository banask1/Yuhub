@echo off
set "MSVC=D:\TOOL\VS Build Tools\VC\Tools\MSVC\14.44.35207"
set "SDKLIB=C:\Program Files (x86)\Windows Kits\10\Lib\10.0.22621.0"
set "SDKINC=C:\Program Files (x86)\Windows Kits\10\Include\10.0.22621.0"
set "PATH=%MSVC%\bin\Hostx64\x64;C:\Users\M1racle\dotnet-sdk;%PATH%"
set "LIB=%MSVC%\lib\x64;%SDKLIB%\um\x64;%SDKLIB%\ucrt\x64"
set "INCLUDE=%MSVC%\include;%SDKINC%\um;%SDKINC%\ucrt;%SDKINC%\shared;%SDKINC%\winrt"
cd /d C:\Users\M1racle\Desktop\Yuhub\accel-framework
dotnet publish -c Release -r win-x64 -o out-aot
