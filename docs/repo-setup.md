# CI

Create the gh pages for helm charts
```sh
git checkout --orphan gh-pages
git reset --hard
echo "# Helm chart repo" > README.md
git add README.md
git commit -m "init gh-pages"
git push origin gh-pages
git checkout -
```

Set up permissions:
- GitHub → Settings → Actions → General → Workflow permissions
- Switch to “Read and write permissions” and Save.