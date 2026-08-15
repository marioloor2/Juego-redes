# Proyectos, versiones y sincronización

## Comportamiento definido

- Cada pulsación de **Guardar nueva versión** crea un registro nuevo.
- Una versión guardada nunca se modifica.
- Cada versión conserva el proyecto completo: geometría, propiedades, actividades,
  zonas rocosas, comentarios y filtros.
- La fecha y hora de guardado se generan automáticamente.
- La fecha de corte puede ser distinta de la fecha de guardado.
- Una versión histórica puede abrirse y utilizarse como origen de otra versión.

## Convención de nombres

Usar el nombre de la obra o urbanización seguido por el tipo de red:

- `Costanera_acacias_AALL`
- `Costanera_acacias_AASS`
- `Costanera_acacias_AAPP`
- `Oryza_AALL`
- `Oryza_AASS`
- `Oryza_AAPP`

La biblioteca local utiliza IndexedDB. Los valores antiguos guardados en
`localStorage` se migran automáticamente a `Proyecto_de_prueba_AALL > v001` la primera
vez que se abre esta versión de la aplicación.

## Copias independientes

**Exportar copia** descarga toda la biblioteca en un JSON. Ese archivo permite
restaurar proyectos y versiones aunque el servicio de nube cambie en el futuro.

La importación reemplaza la biblioteca local únicamente después de mostrar una
confirmación.

## Activar Firebase sin facturación

1. Crear un proyecto en Firebase y mantenerlo en el plan **Spark**.
2. No vincular una cuenta de facturación.
3. Registrar una aplicación web.
4. Activar **Authentication > Google**.
5. Añadir el dominio publicado de la aplicación a los dominios autorizados.
6. Crear una base de datos **Cloud Firestore**.
7. Publicar el contenido de `firestore.rules`.
8. Copiar el objeto de configuración web entregado por Firebase en
   `firebase-config.js`, sustituyendo `null`.

Ejemplo de la forma esperada:

```js
window.RED_VERDE_FIREBASE_CONFIG = {
  apiKey: "...",
  authDomain: "...",
  projectId: "...",
  storageBucket: "...",
  messagingSenderId: "...",
  appId: "..."
};
```

Estos identificadores permiten que el navegador encuentre el proyecto, pero no
son contraseñas. La privacidad depende de las reglas de Firestore y del inicio
de sesión. Las reglas incluidas permiten a cada cuenta leer y escribir
únicamente dentro de su propio espacio.

## Uso entre dispositivos

En el primer dispositivo:

1. Abrir **Proyectos y versiones**.
2. Pulsar **Sincronizar**.
3. Iniciar sesión con Google.
4. Guardar o sincronizar la biblioteca.

En otro dispositivo:

1. Abrir la misma aplicación publicada.
2. Pulsar **Sincronizar** e iniciar sesión con la misma cuenta.
3. Seleccionar el proyecto descargado desde la biblioteca.

Después del primer inicio de sesión, la sesión queda recordada por el navegador.
Cada nueva versión se intenta subir automáticamente al guardarla. Si la nube no
está disponible, la versión permanece en IndexedDB y puede sincronizarse después.

## Capacidad prevista

Con menos de 250 versiones al año, el volumen esperado está muy por debajo de
las cuotas gratuitas. Conviene conservar de todas formas una exportación JSON
periódica, porque el plan gratuito de Firebase no incluye copias de seguridad
administradas.
