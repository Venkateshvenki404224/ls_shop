<script setup>
import { FormControl } from 'frappe-ui'
import RichTextField from '../RichTextField.vue'

defineProps({ product: { type: Object, required: true } })
</script>

<template>
  <section class="space-y-4">
    <FormControl v-model="product.title" label="Title" />
    <RichTextField
      v-model="product.description"
      label="Description"
      description="Shown on the product page."
      min-height="min-h-32"
    />
  </section>
</template>
