# Kalavesi

Kalastuslupien hakupalvelu: hae kohdetta nimellä ja näe, mitä lupia tarvitset, mistä ne saa ja mitä rauhoituksia paikalla on. Pilottialueena on Aurajoen vesistö.

## Kansiot

```
index.html          sivu
data/sites.json     kohteiden tiedot (sivu lukee tämän)
haku/               Python-skripti tietojen keruuseen
```

## 1. Julkaise sivu ilmaiseksi GitHub Pagesissa

Gitiä ei tarvitse osata, kaiken voi tehdä selaimessa.

1. Tee tili osoitteessa https://github.com, jos sinulla ei vielä ole.
2. Valitse oikeasta yläkulmasta **+ → New repository**. Nimeksi esim. `kalavesi`, valitse **Public** ja paina **Create repository**.
3. Valitse **uploading an existing file** ja raahaa koko `kalavesi`-kansion sisältö (index.html, data, haku, README.md) sivulle. Paina **Commit changes**.
4. Avaa **Settings → Pages**. Kohdassa *Build and deployment* valitse **Source: Deploy from a branch**, haaraksi **main** ja kansioksi **/ (root)**. Tallenna.
5. Noin minuutin kuluttua sivu on osoitteessa `https://<käyttäjänimesi>.github.io/kalavesi/`.

Päivitykset tehdään samalla tavalla: lataa muuttunut tiedosto repoon (**Add file → Upload files**), ja sivu päivittyy itsestään.

## 2. Ota käyttöön Maanmittauslaitoksen ilmakuva

Ilman avainta sivu käyttää Esrin ilmakuvaa. MML:n ortokuva on Suomessa tarkempi ja maksuton.

1. Rekisteröidy MML:n Oma tili -palveluun ja luo API-avain (ohje: https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje).
2. Avaa `index.html`, etsi rivi `const MML_API_KEY="";` ja liitä avain lainausmerkkien väliin.
3. Lataa muuttunut `index.html` repoon.

Huomioita:
- Avain näkyy julkisen sivun lähdekoodissa. MML:n avoin rajapinta sallii maksuttoman käytön, mutta ei ole tarkoitettu suurille kävijämäärille. Jos palvelu kasvaa, kysy MML:ltä sopiva ratkaisu.
- Jos kartta jää tyhjäksi avaimen lisäämisen jälkeen, tarkista karttatason nimi MML:n kuvauksesta: https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts/1.0.0/WMTSCapabilities.xml (sivu käyttää tasoa `WGS84_Pseudo-Mercator`).
- Lähdemerkintä (© Maanmittauslaitos) näkyy kartan alakulmassa, kuten lisenssi edellyttää.

## 3. Lisää kohteita

Ilmainen tapa: pyydä Claudea keskustelussa keräämään kalatalousalueen tiedot ja tuottamaan ne `sites.json`-muodossa. Korvaa `data/sites.json` tuloksella ja lataa se repoon.

API-tapa (maksullinen, automaattinen): katso `haku/`-kansion skripti.

```bash
cd haku
pip install -r requirements.txt
export ANTHROPIC_API_KEY=oma-avain
python kalavesi_haku.py --selftest
python kalavesi_haku.py --only halistenkoski
```

Skripti kirjoittaa tulokset suoraan tiedostoon `data/sites.json` ja epävarmat kohteet tiedostoon `haku/tarkistettavat.csv`.

## Tietojen luotettavuus

Jokaisella tiedolla on lähdelinkki ja tarkistuspäivä. Sivu kehottaa aina tarkistamaan ehdot myyjältä ja Kalastusrajoitus.fi:stä.
