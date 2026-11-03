# aicare

CLI Python pour capturer et décoder le protocole Bluetooth Low Energy (BLE)
du saturomètre de pouce **Aicare W013** (SpO2, pouls, indice de perfusion),
dans l'esprit du widget ESPHome « Miscale ».

Objectif : permettre le reverse engineering du protocole puis l'intégration
des mesures dans Home Assistant (via ESPHome ou directement).

## Installation

```bash
pip install .            # CLI seul (commandes offline : decode, guess)
pip install ".[ble]"     # + bleak pour scan/gatt/listen (Bluetooth requis)
```

## Utilisation

### 1. Scanner et trouver l'appareil

```bash
aicare scan
aicare scan --duration 20 --name-filter Aicare
```

### 2. Dumper le GATT complet

```bash
aicare gatt --address AA:BB:CC:DD:EE:FF
```

Affiche tous les services / caractéristiques (UUID + propriétés), et signale
le profil standard **PLX** (`0x1822`) si présent.

### 3. Écouter les mesures en direct

```bash
# découverte automatique de la meilleure caractéristique notify
aicare listen --address AA:BB:CC:DD:EE:FF

# ou ciblage explicite
aicare listen --address AA:BB:CC:DD:EE:FF \
  --service 0000ffff-0000-1000-8000-00805f9b34fb \
  --char 0000fff1-0000-1000-8000-00805f9b34fb
```

Chaque trame est affichée en hexadécimal avec une tentative de décodage
(PLX standard + heuristique sur les octets plausibles SpO2/pouls).

Options utiles pendant la capture :

```bash
# affiche une valeur uniquement si elle est stable sur N trames consécutives
aicare listen --address ... --stable 3

# fige le décodage une fois les offsets connus
aicare listen --address ... --spo2-offset 3 --pulse-offset 4 --pi-offset 5
```

### 4. Décoder une trame hors ligne

```bash
aicare decode 0x0200620048
aicare decode 02:00:62:00:48
```

Tente successivement : profil PLX standard (UUID `0x2A5F`), puis un
scan heuristique des octets qui ressemblent à une SpO2 (50-100 %) et un
pouls (30-250 bpm).

### 5. Corréler des trames avec les valeurs écran

```bash
aicare guess --spo2 98 --pulse 72 02:00:62:00:48 02:00:61:00:70
```

Croise les trames capturées avec les valeurs affichées sur l'écran pour
déduire les offsets exacts du protocole propriétaire. Une fois les offsets
confirmés, le décodage devient déterministe.

### 6. Export pour ESPHome

```bash
aicare listen --address ... --spo2-offset 3 --pulse-offset 4 --pi-offset 5 --json
```

En `--json`, chaque mesure est émise sur une ligne JSON
(`{"spo2": 98, "pulse": 72, "pi": 2.1, ...}`), directement consommable par
un script ou par une automation Home Assistant.

## Décodage PLX standard

Si l'appareil expose le service `0x1822` avec la caractéristique
`PLX Continuous Measurement` (`0x2A5F`), les valeurs sont des **SFLOAT**
(mantisse 12 bits signée + exposant 4 bits signée) :

- SpO2 : octets 2-3
- Pouls : octets 4-5
- Indice de perfusion : présent si le bit 9 des flags (octets 0-1) est à 1

## État du reverse engineering

Le W013 n'expose probablement pas le profil PLX standard (aucun projet
public connu à ce jour). La démarche recommandée :

1. `aicare scan` → récupérer le MAC
2. `aicare gatt` → identifier les services/UUID propriétaires
3. `aicare listen` → capturer des trames pendant 2-3 mesures, en notant
   les valeurs affichées à l'écran
4. `aicare guess --spo2 <valeur> --pulse <valeur> <trames...>` → déduire les
   offsets
5. vérifier avec `aicare listen --spo2-offset N --pulse-offset M`

Si aucune trame n'arrive sans l'application officielle Aicare, un handshake
BLE est nécessaire : capturer un log HCI Android
(`Options développeur > Rapport de bug HCI Bluetooth`) pendant une mesure
dans l'app, puis ouvrir une issue avec le dump.

## Développement

```bash
python -m unittest discover -s tests -v
```

## Licence

MIT
