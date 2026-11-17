module lightphebench

go 1.17

require github.com/serengil/lightphe-go v0.0.0

// relative to this folder: <benchmark repo>/../github/lightphe-go (build.sh overrides it for LIGHTPHE_GITHUB)
replace github.com/serengil/lightphe-go => ../../../github/lightphe-go
