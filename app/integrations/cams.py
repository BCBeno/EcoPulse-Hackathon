from app.integrations.weather import OpenMeteoWeatherProvider


class CamsAirQualityProvider(OpenMeteoWeatherProvider):
    name = "cams"
    endpoint = "https://air-quality-api.open-meteo.com/v1/air-quality"
    variables = "pm2_5,pm10,nitrogen_dioxide,ozone,sulphur_dioxide,european_aqi"
    model = "cams_europe"
